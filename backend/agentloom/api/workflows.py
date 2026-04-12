"""Workflow REST + SSE endpoints (M3).

All reads are scoped to the MVP singleton workspace ``default``. Future
milestones will inject the real workspace via auth middleware.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from agentloom.db.base import get_session
from agentloom.db.models.tenancy import DEFAULT_WORKSPACE_ID
from agentloom.db.repositories.workflow import WorkflowNotFoundError, WorkflowRepository
from agentloom.engine.events import get_event_bus
from agentloom.engine.workflow_engine import WorkflowEngine
from agentloom.schemas import StepKind, WorkFlow, WorkFlowNode
from agentloom.schemas.common import FrozenNodeError
from agentloom.schemas.workflow import WireMessage
from agentloom.tools import default_registry
from agentloom.tools.base import ToolContext

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


def _repo(session: AsyncSession) -> WorkflowRepository:
    return WorkflowRepository(session, workspace_id=DEFAULT_WORKSPACE_ID)


# ---------------------------------------------------------------- Pydantic payloads


class CreateWorkflowResponse(BaseModel):
    id: str


class AppendNodeRequest(BaseModel):
    step_kind: StepKind
    parent_ids: list[str] = Field(default_factory=list)
    description: str = ""
    input_messages: list[WireMessage] | None = None


class AppendNodeResponse(BaseModel):
    node_id: str


# ---------------------------------------------------------------- Routes


@router.post("", response_model=CreateWorkflowResponse)
async def create_workflow(session: AsyncSession = Depends(get_session)) -> CreateWorkflowResponse:
    wf = WorkFlow()
    repo = _repo(session)
    await repo.create(wf)
    await session.commit()
    return CreateWorkflowResponse(id=wf.id)


@router.get("/{workflow_id}")
async def get_workflow(
    workflow_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    repo = _repo(session)
    try:
        wf = await repo.get(workflow_id)
    except WorkflowNotFoundError as exc:
        raise HTTPException(404, f"workflow {workflow_id} not found") from exc
    return wf.model_dump(mode="json")


@router.post("/{workflow_id}/nodes", response_model=AppendNodeResponse)
async def append_node(
    workflow_id: str,
    body: AppendNodeRequest,
    session: AsyncSession = Depends(get_session),
) -> AppendNodeResponse:
    repo = _repo(session)
    try:
        wf = await repo.get(workflow_id)
    except WorkflowNotFoundError as exc:
        raise HTTPException(404, f"workflow {workflow_id} not found") from exc

    node = WorkFlowNode(
        step_kind=body.step_kind,
        parent_ids=list(body.parent_ids),
        input_messages=body.input_messages,
    )
    if body.description:
        from agentloom.schemas.common import EditableText

        node.description = EditableText.by_user(body.description)

    try:
        wf.add_node(node)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    try:
        await repo.save(wf)
    except FrozenNodeError as exc:
        raise HTTPException(409, str(exc)) from exc
    await session.commit()
    return AppendNodeResponse(node_id=node.id)


@router.post("/{workflow_id}/execute")
async def execute_workflow(
    workflow_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    """Execute the workflow synchronously (M3). The response body is the
    fully updated WorkFlow; SSE events also fire for any subscribers.
    """
    from agentloom.providers.types import ChatResponse  # local import, avoid cycles

    repo = _repo(session)
    try:
        wf = await repo.get(workflow_id)
    except WorkflowNotFoundError as exc:
        raise HTTPException(404, f"workflow {workflow_id} not found") from exc

    provider_call = _provider_call_from_settings()
    engine = WorkflowEngine(
        provider_call,
        get_event_bus(),
        tool_registry=default_registry(),
        tool_context=ToolContext(workspace_id=DEFAULT_WORKSPACE_ID),
    )

    try:
        await engine.execute(wf)
    finally:
        # Always persist whatever state the engine produced, even on
        # partial failure, so the caller can inspect via GET.
        try:
            await repo.save(wf)
        except FrozenNodeError:
            # Unreachable under the engine's invariants, but don't swallow
            # a corrupt-state situation silently.
            raise
        await session.commit()

    # Signal end-of-stream to any SSE subscribers.
    await get_event_bus().close(wf.id)

    # Bind ChatResponse reference so the local import isn't unused.
    _ = ChatResponse
    return wf.model_dump(mode="json")


@router.get("/{workflow_id}/events")
async def workflow_events(workflow_id: str) -> EventSourceResponse:
    bus = get_event_bus()

    async def event_stream() -> AsyncIterator[dict]:
        async for event in bus.subscribe(workflow_id):
            yield {"event": event.kind, "data": event.model_dump_json()}

    return EventSourceResponse(event_stream())


# ---------------------------------------------------------------- provider wiring


def _provider_call_from_settings():
    """Return a ProviderCall wired to Volcengine (the only configured
    provider in MVP). Tests override this by patching the symbol.

    Raises RuntimeError at call time if no key is set — that's OK
    because tests always patch before touching the engine.
    """
    from agentloom.config import get_settings
    from agentloom.providers.openai_compat import OpenAICompatAdapter

    settings = get_settings()

    async def call(messages, tools, model):  # type: ignore[no-untyped-def]
        if not settings.volcengine_api_key:
            raise RuntimeError(
                "no provider configured; set VOLCENGINE_API_KEY or "
                "patch agentloom.api.workflows._provider_call_from_settings "
                "in tests"
            )
        adapter = OpenAICompatAdapter(
            friendly_name="volcengine",
            base_url="https://ark.cn-beijing.volces.com/api/coding/v3",
            api_key=settings.volcengine_api_key,
        )
        return await adapter.chat(
            messages=messages,
            tools=tools,
            model=model or "ark-code-latest",
        )

    return call
