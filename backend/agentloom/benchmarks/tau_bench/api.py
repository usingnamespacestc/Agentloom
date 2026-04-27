"""FastAPI router for τ-bench session lifecycle.

Two endpoints (PR 2 of the integration plan):

- ``POST /api/tau-bench/sessions``
  Create a fresh ChatFlow + register a tau-bench session's tools.
  Returns ``{session_id, chatflow_id, instruction, num_tools}``.
  ``session_id`` equals ``chatflow_id`` (1:1 mapping; simplifies
  teardown and keeps the prefix story unambiguous).

- ``POST /api/tau-bench/sessions/{session_id}/teardown``
  Unregister the session's tools and detach the chatflow runtime.
  Idempotent — calling on an unknown session returns ``{ok: true}``.

The runner side (``agentloom_bench/``) calls these out-of-process. Per
``docs/design-tau-bench-integration.md``, GET endpoints for db_state
inspection / reward calc come in PR 3+ together with the runner CLI.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from agentloom.api.chatflows import _get_engine
from agentloom.benchmarks.tau_bench import runtime as tau_runtime
from agentloom.db.base import get_session
from agentloom.db.models.tenancy import DEFAULT_WORKSPACE_ID
from agentloom.db.repositories.chatflow import ChatFlowRepository
from agentloom.mcp.runtime import get_shared_registry
from agentloom.schemas.chatflow import make_chatflow
from agentloom.schemas.common import ProviderModelRef

router = APIRouter(prefix="/api/tau-bench", tags=["tau-bench"])


class CreateSessionRequest(BaseModel):
    domain: str = Field(..., description="``retail`` or ``airline``")
    task_index: int = Field(..., ge=0, description="Index into tau_bench's tasks_test list for the domain")
    agent_model: ProviderModelRef | None = Field(
        None,
        description="Model the spawned ChatFlow should use as its "
        "draft_model. ``None`` falls back to provider's first pinned model.",
    )
    title: str | None = Field(
        None, description="Optional ChatFlow title; default ``[tau-bench] {domain} #{task_index}``"
    )
    tool_loop_budget: int = Field(
        30,
        ge=1,
        description="Max tool-use iterations per turn. Default 30 (vs "
        "the ChatFlow default 12) because airline tasks legitimately "
        "need 5-10+ tool calls per turn (lookup user → list "
        "reservations → for each: get details + update flights + "
        "update baggage). Set lower if you specifically want to "
        "stress-test the budget guard.",
    )
    execution_mode: str | None = Field(
        None,
        description="ChatFlow execution mode override. ``None`` = use the "
        "default (NATIVE_REACT — direct mode, agent loops tool_use until "
        "stop). ``auto_plan`` = full cognitive pipeline (judge_pre → "
        "planner → planner_judge → worker → worker_judge → judge_post). "
        "``semi_auto`` = plan + judges, no auto-decompose. Used for "
        "M7.5+ benchmarking that exercises the cognitive layer.",
    )


class CreateSessionResponse(BaseModel):
    session_id: str
    chatflow_id: str
    domain: str
    task_index: int
    instruction: str = Field(
        ..., description="The task.instruction text — the persona/goal "
        "the runner's user simulator will follow. Backend never feeds this "
        "to the agent directly; it's returned so the runner has it."
    )
    num_tools: int


class TeardownResponse(BaseModel):
    ok: bool
    session_id: str
    unregistered_tools: int


class SessionStateResponse(BaseModel):
    """Snapshot of a session's mock DB. Returned by
    ``GET /api/tau-bench/sessions/{id}/state``.

    The runner pulls this after a task's dialogue completes, then
    feeds it into upstream ``Env.calculate_reward`` (which needs
    ``self.data`` to compute the post-state hash and compare against
    a ground-truth replay of ``task.actions``).

    Snapshots are LARGE — retail's ``orders`` dict alone is ~1.8MB
    serialized. Don't poll this endpoint in a hot loop; one call per
    task at teardown is the intended pattern.
    """

    session_id: str
    domain: str
    data: dict


def _retail_task(task_index: int) -> Any:
    from tau_bench.envs.retail.tasks_test import TASKS_TEST

    if task_index < 0 or task_index >= len(TASKS_TEST):
        raise HTTPException(
            400,
            f"retail task_index {task_index} out of range "
            f"(have {len(TASKS_TEST)} test tasks)",
        )
    return TASKS_TEST[task_index]


def _airline_task(task_index: int) -> Any:
    from tau_bench.envs.airline.tasks_test import TASKS

    if task_index < 0 or task_index >= len(TASKS):
        raise HTTPException(
            400,
            f"airline task_index {task_index} out of range "
            f"(have {len(TASKS)} test tasks)",
        )
    return TASKS[task_index]


@router.post("/sessions", response_model=CreateSessionResponse)
async def create_session(
    body: CreateSessionRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> CreateSessionResponse:
    """Create a benchmark session: fresh ChatFlow + registered tau-bench tools.

    Side effects:
    - Inserts a ChatFlow row in the DB.
    - Mutates the shared ToolRegistry: adds N retail (16) or airline
      (14) wrapper tools under the session prefix.
    - The chatflow's ``disabled_tool_names`` is set to **every** non-tau
      tool currently in the registry, so the agent only sees the
      task-specific tools.
    """
    if body.domain == "retail":
        task = _retail_task(body.task_index)
    elif body.domain == "airline":
        task = _airline_task(body.task_index)
    else:
        raise HTTPException(400, f"unknown domain {body.domain!r}")

    title = body.title or f"[tau-bench] {body.domain} #{body.task_index}"
    chat = make_chatflow(title=title)
    if body.agent_model is not None:
        chat.draft_model = body.agent_model
        # In auto_plan mode the cognitive nodes (judge_pre / planner /
        # worker_judge / judge_post) all default to draft_model. Setting
        # them explicitly here keeps the model surface uniform across
        # the run and makes the benchmark trace easy to attribute.
        chat.default_judge_model = body.agent_model
        chat.default_tool_call_model = body.agent_model
        chat.brief_model = body.agent_model
    # Match the per-call-type override pattern: judge / tool_call / brief
    # all default to draft_model unless overridden later.
    chat.tool_loop_budget = body.tool_loop_budget
    if body.execution_mode is not None:
        from agentloom.schemas.common import ExecutionMode

        try:
            chat.default_execution_mode = ExecutionMode(body.execution_mode)
        except ValueError as exc:
            raise HTTPException(
                400,
                f"unknown execution_mode {body.execution_mode!r}; "
                f"valid: {[m.value for m in ExecutionMode]}",
            ) from exc

    session_id = chat.id  # 1:1 mapping
    try:
        source = tau_runtime.add_session(session_id=session_id, domain=body.domain)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc

    # Disable every non-tau tool so agent only sees the benchmark catalog.
    registry = get_shared_registry()
    tau_names = set(source.registered_names)
    chat.disabled_tool_names = [
        t.name for t in registry.all() if t.name not in tau_names
    ]

    repo = ChatFlowRepository(session, workspace_id=DEFAULT_WORKSPACE_ID)
    await repo.create(chat)
    await session.commit()

    return CreateSessionResponse(
        session_id=session_id,
        chatflow_id=chat.id,
        domain=body.domain,
        task_index=body.task_index,
        instruction=task.instruction or "",
        num_tools=len(source.registered_names),
    )


@router.get(
    "/sessions/{session_id}/state", response_model=SessionStateResponse
)
async def get_session_state(session_id: str) -> SessionStateResponse:
    """Return the current mock DB snapshot for ``session_id``.

    Runner uses this to compute reward locally via upstream
    ``Env.calculate_reward()`` after the dialogue ends. Backend stays
    dumb: just exposes the data dict, doesn't compute reward itself
    (that would force importing more of the upstream surface than
    we vendor).
    """
    src = tau_runtime.get_session(session_id)
    if src is None:
        raise HTTPException(404, f"tau-bench session {session_id!r} not found")
    return SessionStateResponse(
        session_id=session_id,
        domain=src.domain,
        data=src.env_data,
    )


@router.post(
    "/sessions/{session_id}/teardown", response_model=TeardownResponse
)
async def teardown_session(
    session_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TeardownResponse:
    """Unregister a tau-bench session's tools and detach the chatflow
    runtime. Does NOT delete the ChatFlow row — keep it for post-task
    inspection. Caller can DELETE /api/chatflows/{id} separately.

    Idempotent: tearing down an unknown session returns ``ok: true``
    with ``unregistered_tools: 0``.
    """
    src = tau_runtime.get_session(session_id)
    count = len(src.registered_names) if src else 0

    tau_runtime.remove_session(session_id)

    # Detach chatflow runtime if attached so subsequent re-attach reads
    # fresh from DB. ``cancel=True`` cancels any in-flight background
    # tasks the engine may still hold.
    engine = _get_engine(request)
    runtime = engine.get_runtime(session_id)
    if runtime is not None:
        await engine.detach(session_id, cancel=True)

    return TeardownResponse(
        ok=True, session_id=session_id, unregistered_tools=count
    )
