"""ChatFlow REST endpoints (M4 + Round A queue).

Surface:
- ``POST   /api/chatflows``                              create w/ greeting root
- ``GET    /api/chatflows/{id}``                         read full state
- ``POST   /api/chatflows/{id}/turns``                   submit + wait (legacy)
- ``POST   /api/chatflows/{id}/nodes/{nid}/queue``       enqueue pending turn
- ``PATCH  /api/chatflows/{id}/nodes/{nid}/queue/{tid}`` edit queue item
- ``DELETE /api/chatflows/{id}/nodes/{nid}/queue/{tid}`` drop queue item
- ``POST   /api/chatflows/{id}/nodes/{nid}/queue/reorder``
- ``DELETE /api/chatflows/{id}/nodes/{nid}``             delete FAILED node + queue
- ``POST   /api/chatflows/{id}/nodes/{nid}/retry``       retry FAILED node
- ``GET    /api/chatflows/{id}/events``                  SSE stream

All routes scoped to the singleton ``default`` workspace (M21 brings
real workspace auth).

The ChatFlowEngine is a process-lifetime singleton held on
``app.state.chatflow_engine`` — queue operations spawn background
tasks whose runtime state must outlive a single request. Each test
spins up a fresh FastAPI app so the engine is effectively
test-scoped for the test suite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sse_starlette.sse import EventSourceResponse

from agentloom import tenancy_runtime
from agentloom.api import workflows as _workflows_api
from agentloom.db.base import get_session, get_session_scope
from agentloom.db.models.tenancy import DEFAULT_WORKSPACE_ID
from agentloom.db.repositories.chatflow import (
    ChatFlowNotFoundError,
    ChatFlowRepository,
)
from agentloom.db.repositories.provider import ProviderRepository
from agentloom.engine.chatflow_engine import (
    ChatFlowEngine,
    DiscardedUpstreamFailure,
)
from agentloom.engine.events import get_event_bus
from agentloom.schemas import ChatFlow, PendingTurn, make_chatflow
from agentloom.schemas.chatflow import PendingTurnSource
from agentloom.schemas.common import ExecutionMode, FrozenNodeError, ProviderModelRef, StickyNote
from agentloom.mcp.runtime import get_shared_registry
from agentloom.tools.base import ToolContext

router = APIRouter(prefix="/api/chatflows", tags=["chatflows"])


def _repo(session: AsyncSession) -> ChatFlowRepository:
    return ChatFlowRepository(session, workspace_id=DEFAULT_WORKSPACE_ID)


def _provider_repo(session: AsyncSession) -> ProviderRepository:
    return ProviderRepository(session, workspace_id=DEFAULT_WORKSPACE_ID)


async def _resolve_default_model(
    prov_repo: ProviderRepository,
    current: ProviderModelRef | None,
) -> ProviderModelRef | None:
    """Validate `current` against live providers; if valid, return it
    unchanged. If stale or None, pick a new default: first pinned model
    across all providers, else first available model. Returns None if
    no providers / no models are configured.
    """
    providers = await prov_repo.list_all()
    if not providers:
        return None

    if current is not None:
        for p in providers:
            if p["id"] != current.provider_id:
                continue
            for m in p.get("available_models", []):
                if m.get("id") == current.model_id:
                    return current
            break

    for p in providers:
        for m in p.get("available_models", []):
            if m.get("pinned"):
                return ProviderModelRef(provider_id=p["id"], model_id=m["id"])
    for p in providers:
        models = p.get("available_models", [])
        if models:
            return ProviderModelRef(provider_id=p["id"], model_id=models[0]["id"])
    return None


def _get_engine(request: Request) -> ChatFlowEngine:
    """Return the process-lifetime ChatFlowEngine, creating it on first
    request. We stash it on ``app.state`` so integration tests that
    construct a fresh ``FastAPI`` app per test get a fresh engine.
    """
    app = request.app
    engine = getattr(app.state, "chatflow_engine", None)
    if engine is None:
        # Resolve through the module reference so tests that patch
        # ``workflows._provider_call_from_settings`` also affect this route.
        engine = ChatFlowEngine(
            _workflows_api._provider_call_from_settings(),
            get_event_bus(),
            tool_registry=get_shared_registry(),
            tool_context=ToolContext(workspace_id=DEFAULT_WORKSPACE_ID),
        )
        app.state.chatflow_engine = engine
    return engine


async def _attached_chatflow(
    engine: ChatFlowEngine, repo: ChatFlowRepository, chatflow_id: str
) -> ChatFlow:
    """Load a chatflow from DB and attach it to the engine.

    Returns the engine's authoritative in-memory copy: if the engine
    already holds a runtime for this id (from a prior request or a
    background task), we use that one and discard the DB reload —
    the runtime is ahead of the DB by any background-task mutations
    that haven't persisted yet.
    """
    try:
        chat = await repo.get(chatflow_id)
    except ChatFlowNotFoundError as exc:
        raise HTTPException(404, f"chatflow {chatflow_id} not found") from exc
    runtime = await engine.attach(chat)
    return runtime.chatflow


# ---------------------------------------------------------------- schemas


class CreateChatFlowRequest(BaseModel):
    title: str | None = None


class CreateChatFlowResponse(BaseModel):
    id: str


class SubmitTurnRequest(BaseModel):
    text: str
    parent_id: str | None = None
    #: Composer's model pick for this turn. ``None`` → the spawned
    #: ChatNode inherits from its primary parent's ``resolved_model``.
    spawn_model: ProviderModelRef | None = None
    #: Per-kind composer overrides. ``None`` falls back to the
    #: chatflow's default for that kind, then to ``spawn_model``.
    judge_spawn_model: ProviderModelRef | None = None
    tool_call_spawn_model: ProviderModelRef | None = None


class SubmitTurnResponse(BaseModel):
    node_id: str
    status: str
    agent_response: str


class EnqueueRequest(BaseModel):
    text: str
    source: PendingTurnSource = "web"
    spawn_model: ProviderModelRef | None = None
    judge_spawn_model: ProviderModelRef | None = None
    tool_call_spawn_model: ProviderModelRef | None = None


class PendingTurnPayload(BaseModel):
    id: str
    text: str
    source: PendingTurnSource

    @classmethod
    def from_model(cls, pending: PendingTurn) -> "PendingTurnPayload":
        return cls(id=pending.id, text=pending.text, source=pending.source)


class PatchQueueItemRequest(BaseModel):
    text: str


class ReorderQueueRequest(BaseModel):
    item_ids: list[str]


class RetryRequest(BaseModel):
    spawn_model: ProviderModelRef | None = None
    judge_spawn_model: ProviderModelRef | None = None
    tool_call_spawn_model: ProviderModelRef | None = None


class RetryResponse(BaseModel):
    node_id: str


class NodePosition(BaseModel):
    id: str
    x: float
    y: float


class PatchPositionsRequest(BaseModel):
    positions: list[NodePosition]


# ---------------------------------------------------------------- routes


@router.get("")
async def list_chatflows(
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """Return lightweight summaries of all chatflows (no full payload)."""
    return await _repo(session).list_summaries()


@router.post("", response_model=CreateChatFlowResponse)
async def create_chatflow(
    body: CreateChatFlowRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> CreateChatFlowResponse:
    chat = make_chatflow(title=(body.title if body else None))
    prov_repo = _provider_repo(session)
    chat.default_model = await _resolve_default_model(prov_repo, None)
    # Pre-fill disabled_tool_names from the workspace tool_states so
    # tools marked ``available`` or ``disabled`` stay unchecked in the
    # ChatFlow settings picker. ``default_allow`` tools are omitted so
    # they remain visible by default.
    ws_settings = tenancy_runtime.get_settings(DEFAULT_WORKSPACE_ID)
    registered_names = [t.name for t in get_shared_registry().all()]
    chat.disabled_tool_names = ws_settings.pre_disabled_for_new_chatflow(
        registered_names
    )
    await _repo(session).create(chat)
    await session.commit()
    return CreateChatFlowResponse(id=chat.id)


@router.get("/{chatflow_id}")
async def get_chatflow(
    chatflow_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    # If a runtime is already attached, it's authoritative — prefer
    # its in-memory copy over the stale DB row so the client sees
    # mutations produced by background tasks that haven't persisted.
    engine = _get_engine(request)
    runtime = engine.get_runtime(chatflow_id)
    if runtime is not None:
        return runtime.chatflow.model_dump(mode="json")

    repo = _repo(session)
    try:
        chat = await repo.get(chatflow_id)
    except ChatFlowNotFoundError as exc:
        raise HTTPException(404, f"chatflow {chatflow_id} not found") from exc

    # Lazy-rehydrate stale default (provider/model may have been
    # deleted since the chatflow was last opened).
    prov_repo = _provider_repo(session)
    new_model = await _resolve_default_model(prov_repo, chat.default_model)
    if new_model != chat.default_model:
        chat.default_model = new_model
        await repo.patch_metadata(chatflow_id, default_model=new_model)
        await session.commit()
    return chat.model_dump(mode="json")


@router.delete("/{chatflow_id}")
async def delete_chatflow(
    chatflow_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Delete an entire chatflow."""
    # Detach from engine if it has a runtime.
    engine = _get_engine(request)
    runtime = engine.get_runtime(chatflow_id)
    if runtime is not None:
        await engine.detach(chatflow_id)
    repo = _repo(session)
    try:
        await repo.delete(chatflow_id)
    except ChatFlowNotFoundError as exc:
        raise HTTPException(404, f"chatflow {chatflow_id} not found") from exc
    await session.commit()
    return {"ok": True}


class PatchChatFlowRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    default_model: ProviderModelRef | None = None
    default_judge_model: ProviderModelRef | None = None
    default_tool_call_model: ProviderModelRef | None = None
    default_execution_mode: ExecutionMode | None = None
    judge_retry_budget: int | None = None
    min_ground_ratio: float | None = None
    ground_ratio_grace_nodes: int | None = None
    disabled_tool_names: list[str] | None = None


@router.patch("/{chatflow_id}")
async def patch_chatflow(
    chatflow_id: str,
    body: PatchChatFlowRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    repo = _repo(session)
    provided = body.model_fields_set
    kwargs: dict = {}
    if "title" in provided:
        kwargs["title"] = body.title
    if "description" in provided:
        kwargs["description"] = body.description
    if "tags" in provided:
        kwargs["tags"] = body.tags
    if "default_model" in provided:
        kwargs["default_model"] = body.default_model
    if "default_judge_model" in provided:
        kwargs["default_judge_model"] = body.default_judge_model
    if "default_tool_call_model" in provided:
        kwargs["default_tool_call_model"] = body.default_tool_call_model
    if "default_execution_mode" in provided:
        kwargs["default_execution_mode"] = body.default_execution_mode
    if "judge_retry_budget" in provided:
        kwargs["judge_retry_budget"] = body.judge_retry_budget
    if "min_ground_ratio" in provided:
        kwargs["min_ground_ratio"] = body.min_ground_ratio
    if "ground_ratio_grace_nodes" in provided:
        kwargs["ground_ratio_grace_nodes"] = body.ground_ratio_grace_nodes
    if "disabled_tool_names" in provided:
        kwargs["disabled_tool_names"] = body.disabled_tool_names
    if not kwargs:
        return {"ok": True}
    try:
        await repo.patch_metadata(chatflow_id, **kwargs)
    except ChatFlowNotFoundError as exc:
        raise HTTPException(404, f"chatflow {chatflow_id} not found") from exc
    await session.commit()

    # Mirror into the engine's in-memory runtime so subsequent GETs
    # and turn submissions see the new values (the runtime is
    # authoritative over the DB while attached).
    runtime = _get_engine(request).get_runtime(chatflow_id)
    if runtime is not None:
        rt_chat = runtime.chatflow
        if "title" in provided:
            rt_chat.title = body.title
        if "description" in provided:
            rt_chat.description = body.description
        if "tags" in provided:
            rt_chat.tags = body.tags or []
        if "default_model" in provided:
            rt_chat.default_model = body.default_model
        if "default_judge_model" in provided:
            rt_chat.default_judge_model = body.default_judge_model
        if "default_tool_call_model" in provided:
            rt_chat.default_tool_call_model = body.default_tool_call_model
        if "default_execution_mode" in provided and body.default_execution_mode is not None:
            rt_chat.default_execution_mode = body.default_execution_mode
        if "judge_retry_budget" in provided and body.judge_retry_budget is not None:
            rt_chat.judge_retry_budget = body.judge_retry_budget
        if "min_ground_ratio" in provided:
            # None is a legal value here (= disable the fuse), so we
            # mirror it through verbatim rather than checking not None.
            rt_chat.min_ground_ratio = body.min_ground_ratio
        if (
            "ground_ratio_grace_nodes" in provided
            and body.ground_ratio_grace_nodes is not None
        ):
            rt_chat.ground_ratio_grace_nodes = body.ground_ratio_grace_nodes
        if "disabled_tool_names" in provided and body.disabled_tool_names is not None:
            rt_chat.disabled_tool_names = body.disabled_tool_names

    return {"ok": True}


class MoveFolderRequest(BaseModel):
    folder_id: str | None = None


@router.patch("/{chatflow_id}/folder")
async def move_chatflow_to_folder(
    chatflow_id: str,
    body: MoveFolderRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    repo = _repo(session)
    try:
        await repo.move_to_folder(chatflow_id, body.folder_id)
    except ChatFlowNotFoundError as exc:
        raise HTTPException(404, f"chatflow {chatflow_id} not found") from exc
    await session.commit()
    return {"ok": True}


@router.patch("/{chatflow_id}/positions")
async def patch_positions(
    chatflow_id: str,
    body: PatchPositionsRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    engine = _get_engine(request)
    repo = _repo(session)
    chat = await _attached_chatflow(engine, repo, chatflow_id)
    for pos in body.positions:
        node = chat.nodes.get(pos.id)
        if node is not None:
            node.position_x = pos.x
            node.position_y = pos.y
    await repo.save(chat)
    await session.commit()
    return {"ok": True}


@router.patch("/{chatflow_id}/nodes/{chat_node_id}/workflow/positions")
async def patch_workflow_positions(
    chatflow_id: str,
    chat_node_id: str,
    body: PatchPositionsRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Persist user-dragged positions for WorkNodes inside a ChatNode's
    inner WorkFlow. Mirrors :func:`patch_positions` but scopes the lookup
    to the named inner WorkFlow."""
    engine = _get_engine(request)
    repo = _repo(session)
    chat = await _attached_chatflow(engine, repo, chatflow_id)
    chat_node = chat.nodes.get(chat_node_id)
    if chat_node is None:
        raise HTTPException(404, f"chat node {chat_node_id} not found")
    inner = chat_node.workflow.nodes
    for pos in body.positions:
        node = inner.get(pos.id)
        if node is not None:
            node.position_x = pos.x
            node.position_y = pos.y
    await repo.save(chat)
    await session.commit()
    return {"ok": True}


class PatchStickyNotesRequest(BaseModel):
    notes: dict[str, StickyNote]
    sub_path: list[str] = []


def _resolve_workflow(chat: "ChatFlow", chat_node_id: str, sub_path: list[str]):
    """Walk chat_node → workflow, then optionally into nested sub-workflows via sub_path."""
    from agentloom.schemas.workflow import WorkFlow
    chat_node = chat.nodes.get(chat_node_id)
    if chat_node is None:
        raise HTTPException(404, f"chat node {chat_node_id} not found")
    wf: WorkFlow = chat_node.workflow
    for work_node_id in sub_path:
        wn = wf.nodes.get(work_node_id)
        if wn is None or wn.sub_workflow is None:
            raise HTTPException(404, f"work node {work_node_id} has no sub_workflow")
        wf = wn.sub_workflow
    return wf


@router.put("/{chatflow_id}/sticky-notes")
async def put_sticky_notes(
    chatflow_id: str,
    body: PatchStickyNotesRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    engine = _get_engine(request)
    repo = _repo(session)
    chat = await _attached_chatflow(engine, repo, chatflow_id)
    chat.sticky_notes = body.notes
    await repo.save(chat)
    await session.commit()
    return {"ok": True}


@router.put("/{chatflow_id}/nodes/{chat_node_id}/workflow/sticky-notes")
async def put_workflow_sticky_notes(
    chatflow_id: str,
    chat_node_id: str,
    body: PatchStickyNotesRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    engine = _get_engine(request)
    repo = _repo(session)
    chat = await _attached_chatflow(engine, repo, chatflow_id)
    wf = _resolve_workflow(chat, chat_node_id, body.sub_path)
    wf.sticky_notes = body.notes
    await repo.save(chat)
    await session.commit()
    return {"ok": True}


@router.post("/{chatflow_id}/turns", response_model=SubmitTurnResponse)
async def submit_turn(
    chatflow_id: str,
    body: SubmitTurnRequest,
    request: Request,
    session_maker: async_sessionmaker[AsyncSession] = Depends(get_session_scope),
) -> SubmitTurnResponse:
    """Submit a user turn and block until the spawned ChatNode finishes.

    Session lifecycle is split into two short-lived scopes around the
    long ``submit_user_turn`` await. A single ``Depends(get_session)``
    would pin one DB connection for the entire workflow run (minutes
    to hours on semi_auto/auto chains), which under the default pool
    size quickly exhausts the pool and causes unrelated endpoints
    (e.g. GET /api/providers) to 500 with QueuePool timeouts. The
    engine itself never touches the DB — persistence is handler-side
    — so releasing the session between phases is safe.
    """
    engine = _get_engine(request)

    # Phase 1: load + attach (short session)
    async with session_maker() as session:
        repo = _repo(session)
        chat = await _attached_chatflow(engine, repo, chatflow_id)

    # Phase 2: run the turn — no DB connection held
    try:
        chat_node = await engine.submit_user_turn(
            chat,
            body.text,
            parent_id=body.parent_id,
            spawn_model=body.spawn_model,
            judge_spawn_model=body.judge_spawn_model,
            tool_call_spawn_model=body.tool_call_spawn_model,
        )
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc
    except DiscardedUpstreamFailure as exc:
        raise HTTPException(409, str(exc)) from exc

    # Phase 3: persist the final state (fresh short session)
    async with session_maker() as session:
        await _repo(session).save(chat)
        await session.commit()

    return SubmitTurnResponse(
        node_id=chat_node.id,
        status=chat_node.status.value,
        agent_response=chat_node.agent_response.text,
    )


@router.post(
    "/{chatflow_id}/nodes/{node_id}/queue",
    response_model=PendingTurnPayload,
)
async def enqueue_queue_item(
    chatflow_id: str,
    node_id: str,
    body: EnqueueRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> PendingTurnPayload:
    engine = _get_engine(request)
    repo = _repo(session)
    chat = await _attached_chatflow(engine, repo, chatflow_id)
    if node_id not in chat.nodes:
        raise HTTPException(404, f"node {node_id} not in chatflow {chatflow_id}")
    pending = await engine.enqueue(
        chat.id,
        node_id,
        body.text,
        source=body.source,
        spawn_model=body.spawn_model,
        judge_spawn_model=body.judge_spawn_model,
        tool_call_spawn_model=body.tool_call_spawn_model,
    )
    await repo.save(chat)
    await session.commit()
    return PendingTurnPayload.from_model(pending)


@router.patch("/{chatflow_id}/nodes/{node_id}/queue/{item_id}")
async def patch_queue_item(
    chatflow_id: str,
    node_id: str,
    item_id: str,
    body: PatchQueueItemRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    engine = _get_engine(request)
    repo = _repo(session)
    chat = await _attached_chatflow(engine, repo, chatflow_id)
    if node_id not in chat.nodes:
        raise HTTPException(404, f"node {node_id} not in chatflow {chatflow_id}")
    try:
        await engine.patch_queue_item(chat.id, node_id, item_id, body.text)
    except KeyError as exc:
        raise HTTPException(404, f"queue item {item_id} not found") from exc
    await repo.save(chat)
    await session.commit()
    return {"ok": True}


@router.delete("/{chatflow_id}/nodes/{node_id}/queue/{item_id}")
async def delete_queue_item(
    chatflow_id: str,
    node_id: str,
    item_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    engine = _get_engine(request)
    repo = _repo(session)
    chat = await _attached_chatflow(engine, repo, chatflow_id)
    if node_id not in chat.nodes:
        raise HTTPException(404, f"node {node_id} not in chatflow {chatflow_id}")
    try:
        await engine.delete_queue_item(chat.id, node_id, item_id)
    except KeyError as exc:
        raise HTTPException(404, f"queue item {item_id} not found") from exc
    await repo.save(chat)
    await session.commit()
    return {"ok": True}


@router.post("/{chatflow_id}/nodes/{node_id}/queue/reorder")
async def reorder_queue(
    chatflow_id: str,
    node_id: str,
    body: ReorderQueueRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    engine = _get_engine(request)
    repo = _repo(session)
    chat = await _attached_chatflow(engine, repo, chatflow_id)
    if node_id not in chat.nodes:
        raise HTTPException(404, f"node {node_id} not in chatflow {chatflow_id}")
    try:
        await engine.reorder_queue(chat.id, node_id, body.item_ids)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await repo.save(chat)
    await session.commit()
    return {"ok": True}


@router.delete("/{chatflow_id}/nodes/{node_id}")
async def delete_node(
    chatflow_id: str,
    node_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Delete a node and all its descendants (cascade).

    Returns 409 if any node in the subtree is currently RUNNING.
    """
    engine = _get_engine(request)
    repo = _repo(session)
    chat = await _attached_chatflow(engine, repo, chatflow_id)
    if node_id not in chat.nodes:
        raise HTTPException(404, f"node {node_id} not in chatflow {chatflow_id}")
    try:
        removed = await engine.delete_node_cascade(chat.id, node_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        await repo.save(chat)
    except FrozenNodeError as exc:
        raise HTTPException(409, str(exc)) from exc
    await session.commit()
    return {"ok": True, "removed": removed}


@router.post("/{chatflow_id}/nodes/{node_id}/retry", response_model=RetryResponse)
async def retry_failed_node(
    chatflow_id: str,
    node_id: str,
    body: RetryRequest | None = None,
    request: Request = None,  # type: ignore[assignment]
    session: AsyncSession = Depends(get_session),
) -> RetryResponse:
    engine = _get_engine(request)
    repo = _repo(session)
    chat = await _attached_chatflow(engine, repo, chatflow_id)
    if node_id not in chat.nodes:
        raise HTTPException(404, f"node {node_id} not in chatflow {chatflow_id}")
    body = body or RetryRequest()
    try:
        sibling = await engine.retry_failed_node(
            chat.id,
            node_id,
            spawn_model=body.spawn_model,
            judge_spawn_model=body.judge_spawn_model,
            tool_call_spawn_model=body.tool_call_spawn_model,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await repo.save(chat)
    await session.commit()
    return RetryResponse(node_id=sibling.id)


@router.post("/{chatflow_id}/nodes/{node_id}/cancel")
async def cancel_running_node(
    chatflow_id: str,
    node_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    engine = _get_engine(request)
    repo = _repo(session)
    chat = await _attached_chatflow(engine, repo, chatflow_id)
    if node_id not in chat.nodes:
        raise HTTPException(404, f"node {node_id} not in chatflow {chatflow_id}")
    await engine.cancel_running_node(chat.id, node_id)
    await repo.save(chat)
    await session.commit()
    return {"ok": True}


@router.get("/{chatflow_id}/events")
async def chatflow_events(chatflow_id: str) -> EventSourceResponse:
    bus = get_event_bus()

    async def event_stream() -> AsyncIterator[dict]:
        async for event in bus.subscribe(chatflow_id):
            yield {"event": event.kind, "data": event.model_dump_json()}

    return EventSourceResponse(event_stream())
