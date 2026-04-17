"""Workflow REST + SSE endpoints (M3).

All reads are scoped to the MVP singleton workspace ``default``. Future
milestones will inject the real workspace via auth middleware.
"""

from __future__ import annotations

import asyncio
import logging
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
from agentloom.schemas.common import FrozenNodeError, JudgeVariant
from agentloom.schemas.workflow import WireMessage
from agentloom.mcp.runtime import get_shared_registry
from agentloom.tools.base import ToolContext

log = logging.getLogger(__name__)

#: Tracks background re-run tasks so tests can await completion
#: deterministically. Production traffic ignores this set — tasks
#: remove themselves on completion, so the set is always ≤ the number
#: of in-flight re-runs.
_active_judge_tasks: set[asyncio.Task] = set()


def _spawn_judge_task(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _active_judge_tasks.add(task)
    task.add_done_callback(_active_judge_tasks.discard)
    return task


async def drain_judge_tasks() -> None:
    """Awaitable for tests: block until every re-run task fired from
    this module finishes. Production code never calls this."""
    while _active_judge_tasks:
        await asyncio.gather(*list(_active_judge_tasks), return_exceptions=True)

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
        tool_registry=get_shared_registry(),
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


class RerunJudgeRequest(BaseModel):
    """Optional overrides for a re-run. When omitted the new judge_call
    inherits sensible defaults from the existing graph (see handler)."""

    parent_ids: list[str] | None = None
    input_messages: list[WireMessage] | None = None


class RerunJudgeResponse(BaseModel):
    """202-style response — the judge runs asynchronously and streams
    its verdict over the existing SSE channel at
    ``GET /api/workflows/{workflow_id}/events``. Clients poll
    ``GET /api/workflows/{workflow_id}`` or watch SSE for the result."""

    judge_node_id: str


async def _run_judge_in_background(workflow_id: str, new_node_id: str) -> None:
    """Load the workflow in a fresh session, run the engine (which
    will run exactly the new PLANNED judge_call since every other node
    is already frozen), and persist the result. Errors are logged —
    they also land on the node via the engine's normal failure path,
    so the client sees them via SSE + GET."""
    from agentloom.db.base import get_session_maker

    try:
        async with get_session_maker()() as session:
            repo = WorkflowRepository(session, workspace_id=DEFAULT_WORKSPACE_ID)
            wf = await repo.get(workflow_id)
            engine = WorkflowEngine(
                _provider_call_from_settings(),
                get_event_bus(),
                tool_registry=get_shared_registry(),
                tool_context=ToolContext(workspace_id=DEFAULT_WORKSPACE_ID),
            )
            await engine.execute(wf)
            await repo.save(wf)
            await session.commit()
    except Exception:  # noqa: BLE001 — background boundary
        log.exception(
            "re-run judge failed for workflow=%s node=%s", workflow_id, new_node_id
        )
    finally:
        await get_event_bus().close(workflow_id)


def _latest_same_variant_judge(
    wf: WorkFlow, variant: JudgeVariant
) -> WorkFlowNode | None:
    """Find the most recently created judge_call of the same variant to
    copy ``input_messages`` / ``parent_ids`` from. Returns None if no
    prior judge of this variant exists in the WorkFlow."""
    candidates = [
        n
        for n in wf.nodes.values()
        if n.step_kind == StepKind.JUDGE_CALL and n.judge_variant == variant
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda n: n.created_at)
    return candidates[-1]


@router.post(
    "/{workflow_id}/judge/{variant}",
    response_model=RerunJudgeResponse,
    status_code=202,
)
async def rerun_workflow_judge(
    workflow_id: str,
    variant: JudgeVariant,
    body: RerunJudgeRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> RerunJudgeResponse:
    """Create a sibling ``judge_call`` at WorkFlow scope and schedule
    it. Re-runs never mutate the original judge — the evaluation
    history is preserved in the DAG (ADR-018)."""
    repo = _repo(session)
    try:
        wf = await repo.get(workflow_id)
    except WorkflowNotFoundError as exc:
        raise HTTPException(404, f"workflow {workflow_id} not found") from exc

    prior = _latest_same_variant_judge(wf, variant)
    body = body or RerunJudgeRequest()
    parent_ids = list(
        body.parent_ids
        if body.parent_ids is not None
        else (prior.parent_ids if prior else [])
    )
    input_messages = body.input_messages or (prior.input_messages if prior else None)

    new_node = WorkFlowNode(
        step_kind=StepKind.JUDGE_CALL,
        judge_variant=variant,
        parent_ids=parent_ids,
        input_messages=input_messages,
        judge_target_id=prior.judge_target_id if prior else None,
    )
    try:
        wf.add_node(new_node)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    await repo.save(wf)
    await session.commit()

    _spawn_judge_task(_run_judge_in_background(workflow_id, new_node.id))
    return RerunJudgeResponse(judge_node_id=new_node.id)


@router.post(
    "/{workflow_id}/nodes/{node_id}/judge/{variant}",
    response_model=RerunJudgeResponse,
    status_code=202,
)
async def rerun_node_judge(
    workflow_id: str,
    node_id: str,
    variant: JudgeVariant,
    body: RerunJudgeRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> RerunJudgeResponse:
    """Create a sibling ``judge_call`` pointed at ``node_id`` and
    schedule it. Equivalent to the WorkFlow-level endpoint but with
    ``judge_target_id`` pinned to the specific node and its I/O in the
    new judge's ancestor chain."""
    repo = _repo(session)
    try:
        wf = await repo.get(workflow_id)
    except WorkflowNotFoundError as exc:
        raise HTTPException(404, f"workflow {workflow_id} not found") from exc

    if node_id not in wf.nodes:
        raise HTTPException(404, f"node {node_id} not in workflow {workflow_id}")

    body = body or RerunJudgeRequest()
    new_node = WorkFlowNode(
        step_kind=StepKind.JUDGE_CALL,
        judge_variant=variant,
        parent_ids=list(body.parent_ids) if body.parent_ids is not None else [node_id],
        input_messages=body.input_messages,
        judge_target_id=node_id,
    )
    try:
        wf.add_node(new_node)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    await repo.save(wf)
    await session.commit()

    _spawn_judge_task(_run_judge_in_background(workflow_id, new_node.id))
    return RerunJudgeResponse(judge_node_id=new_node.id)


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

    async def call(messages, tools, model, on_token=None, extra=None, json_schema=None):  # type: ignore[no-untyped-def]
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

            call_extra: dict = {}
            # Volcengine needs explicit thinking enable.
            if "volces.com" in config.base_url or "volcengine" in config.friendly_name.lower():
                call_extra = {"thinking": {"type": "enabled"}}
            if extra:
                call_extra.update(extra)

            adapter = build_adapter(
                kind=config.provider_kind.value,
                friendly_name=config.friendly_name,
                base_url=config.base_url,
                api_key=api_key,
                sub_kind=config.provider_sub_kind.value if config.provider_sub_kind else None,
            )
            # Use first pinned model as default if no model specified.
            if not model_id:
                pinned_models = config.pinned_models()
                fallback = pinned_models[0] if pinned_models else (
                    config.available_models[0] if config.available_models else None
                )
                model_id = fallback.id if fallback else None

            # Look up model metadata to enforce output length limits.
            # max_output_tokens is the precise cap; context_window is
            # the total budget (prompt + completion) and serves as a
            # generous fallback when the per-model output cap isn't set.
            model_info = next(
                (m for m in config.available_models if m.id == model_id),
                None,
            )
            _FALLBACK_MAX_TOKENS = 8192
            max_tokens: int = _FALLBACK_MAX_TOKENS
            if model_info is not None:
                max_tokens = (
                    model_info.max_output_tokens
                    or model_info.context_window
                    or _FALLBACK_MAX_TOKENS
                )

            # Resolve structured-output discipline: per-model override
            # wins; otherwise the provider-level default. ``"none"``
            # (the default default) skips response_format altogether.
            resolved_json_mode = None
            if model_info is not None and model_info.json_mode is not None:
                resolved_json_mode = model_info.json_mode.value
            elif config.json_mode is not None:
                resolved_json_mode = config.json_mode.value

            # Per-model sampling parameters; ``None`` falls through to
            # adapter → provider default. No provider-level default here
            # (scoped to ModelInfo only). Schema-layer validation
            # (ProviderConfig._validate_sub_kind_params) guarantees that
            # only params legal for this sub_kind are ever non-None.
            temperature = model_info.temperature if model_info else None
            top_p = model_info.top_p if model_info else None
            top_k = model_info.top_k if model_info else None
            presence_penalty = model_info.presence_penalty if model_info else None
            frequency_penalty = model_info.frequency_penalty if model_info else None
            repetition_penalty = model_info.repetition_penalty if model_info else None
            num_ctx = model_info.num_ctx if model_info else None
            thinking_budget_tokens = (
                model_info.thinking_budget_tokens if model_info else None
            )

            return await adapter.chat(
                messages=messages,
                tools=tools,
                model=model_id,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                presence_penalty=presence_penalty,
                frequency_penalty=frequency_penalty,
                repetition_penalty=repetition_penalty,
                num_ctx=num_ctx,
                thinking_budget_tokens=thinking_budget_tokens,
                max_tokens=max_tokens,
                extra=call_extra or None,
                on_token=on_token,
                json_mode=resolved_json_mode,
                json_schema=json_schema,
            )

    return call
