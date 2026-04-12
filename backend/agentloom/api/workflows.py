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
    """Return a ProviderCall that resolves the provider dynamically.

    Resolution order:
    1. If a ``model`` string is passed in the format ``provider_id:model_id``,
       look up that provider from the DB.
    2. Otherwise, use the first provider registered in the DB.

    Tests override this by patching the symbol.
    """
    from agentloom.db.base import get_session_maker
    from agentloom.db.models.tenancy import DEFAULT_WORKSPACE_ID
    from agentloom.db.repositories.provider import ProviderRepository
    from agentloom.providers.registry import build_adapter

    async def call(messages, tools, model):  # type: ignore[no-untyped-def]
        async with get_session_maker()() as session:
            repo = ProviderRepository(session, workspace_id=DEFAULT_WORKSPACE_ID)
            providers = await repo.list_all()

            if not providers:
                raise RuntimeError(
                    "no provider configured; add one via the settings page, "
                    "or patch agentloom.api.workflows._provider_call_from_settings "
                    "in tests"
                )

            # If model contains ":", treat prefix as provider_id.
            provider_id = None
            model_id = model
            if model and ":" in model:
                provider_id, model_id = model.split(":", 1)

            chosen = None
            if provider_id:
                chosen = next((p for p in providers if p["id"] == provider_id), None)
            if chosen is None:
                chosen = providers[0]

            config = await repo.get(chosen["id"])
            api_key = repo.resolve_api_key(config)

            extra: dict = {}
            # Volcengine needs explicit thinking enable.
            if "volces.com" in config.base_url or "volcengine" in config.friendly_name.lower():
                extra = {"thinking": {"type": "enabled"}}

            adapter = build_adapter(
                kind=config.provider_kind.value,
                friendly_name=config.friendly_name,
                base_url=config.base_url,
                api_key=api_key,
            )
            # Use first pinned model as default if no model specified.
            if not model_id:
                pinned_models = config.pinned_models()
                fallback = pinned_models[0] if pinned_models else (
                    config.available_models[0] if config.available_models else None
                )
                model_id = fallback.id if fallback else None

            return await adapter.chat(
                messages=messages,
                tools=tools,
                model=model_id,
                extra=extra or None,
            )

    return call
