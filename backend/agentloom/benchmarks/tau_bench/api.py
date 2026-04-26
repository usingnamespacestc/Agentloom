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
    # Match the per-call-type override pattern: judge / tool_call / brief
    # all default to draft_model unless overridden later.

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
