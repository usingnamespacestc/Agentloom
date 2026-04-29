"""Single-task runner that drives one τ-bench session through a
multi-turn dialogue: user_simulator ↔ Agentloom backend.

Loop semantics:

1. Backend create_session → get chatflow_id + task instruction.
2. user_simulator.reset(instruction) → first user message.
3. While not stop:
     - submit_turn(message) → agent_response
     - check stop tokens (``###STOP###`` from upstream tau_bench, or
       ``###TRANSFER###``) — both directions
     - user_simulator.step(agent_response) → next user message
4. Backend teardown_session.
5. Return :class:`TaskTrace` (full turn list + metadata).

The user simulator is parameterized as a :class:`UserSimulator`
Protocol so test code can stub it without importing upstream
``tau_bench.envs.user``. Real runs use
:func:`adapt_tau_bench_user_strategy` (PR 4) which wraps
``tau_bench.envs.user.LLMUserSimulationEnv`` etc.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .client import SessionInfo, TauBenchBackendClient


# Stop tokens emitted by upstream tau_bench user simulator when the
# task is judged complete or beyond the simulator's reach. Either
# side may emit them — we check both.
_STOP_TOKENS = ("###STOP###", "###TRANSFER###")


def _has_stop_token(text: str) -> bool:
    return any(tok in text for tok in _STOP_TOKENS)


class UserSimulator(Protocol):
    """Subset of upstream ``tau_bench.envs.user.BaseUserSimulationEnv``
    that the runner depends on. Production runs inject the real LLM-
    backed strategy; tests inject a deterministic stub.
    """

    def reset(self, instruction: str) -> str:
        """Initialize with the task instruction; returns first user msg."""
        ...

    def step(self, content: str) -> str:
        """Given the agent's last reply, return the user's next msg."""
        ...


@dataclass
class TurnRecord:
    role: str  # "user" or "agent"
    text: str
    node_id: str | None = None  # backend node id for agent turns
    duration_seconds: float | None = None  # only set for agent turns


@dataclass
class TaskTrace:
    """Full record of one task run, returned by :meth:`TauBenchRunner.run_task`."""

    domain: str
    task_index: int
    session_id: str
    chatflow_id: str
    instruction: str
    turns: list[TurnRecord] = field(default_factory=list)
    stop_reason: str = "unknown"
    total_duration_seconds: float = 0.0
    error: str | None = None
    #: Mock DB snapshot fetched right before backend teardown (when
    #: ``run_task(fetch_final_state=True)``). Reward computation
    #: needs this; pure plumbing tests can leave it ``None``.
    final_state: dict | None = None


class TauBenchRunner:
    """Run a single task to completion. Stateless across runs — call
    :meth:`run_task` repeatedly with the same runner instance."""

    def __init__(
        self,
        backend: TauBenchBackendClient,
        *,
        max_turns: int = 30,
        no_progress_threshold: int = 3,
    ) -> None:
        self._backend = backend
        self._max_turns = max_turns
        self._no_progress_threshold = no_progress_threshold

    async def run_task(
        self,
        *,
        domain: str,
        task_index: int,
        user_simulator: UserSimulator,
        agent_model: dict[str, str] | None = None,
        fetch_final_state: bool = True,
        execution_mode: str | None = None,
    ) -> TaskTrace:
        """Drive one τ-bench task to completion.

        ``fetch_final_state=True`` pulls the backend's mock DB snapshot
        right before teardown. The runner needs this to compute reward
        (DB hash comparison via ``calculate_reward``). Set ``False``
        for plumbing-only smoke runs to skip the network round-trip
        and the dict copy.
        """
        t0 = time.monotonic()
        session = await self._backend.create_session(
            domain=domain,
            task_index=task_index,
            agent_model=agent_model,
            execution_mode=execution_mode,
        )
        trace = TaskTrace(
            domain=session.domain,
            task_index=session.task_index,
            session_id=session.session_id,
            chatflow_id=session.chatflow_id,
            instruction=session.instruction,
        )

        try:
            user_msg = user_simulator.reset(session.instruction)
        except Exception as exc:  # noqa: BLE001 — capture cleanly so we still teardown
            trace.error = f"user_simulator.reset failed: {exc!r}"
            await self._teardown_quiet(session)
            trace.stop_reason = "user_simulator_reset_error"
            trace.total_duration_seconds = time.monotonic() - t0
            return trace

        try:
            await self._loop(trace, session, user_simulator, user_msg)
        finally:
            # Pull final state BEFORE teardown — once teardown runs,
            # the env_data dict in backend's source registry is
            # released and unreachable.
            if fetch_final_state:
                try:
                    state_resp = await self._backend.get_session_state(
                        session.session_id
                    )
                    trace.final_state = state_resp.get("data")
                except Exception as exc:  # noqa: BLE001
                    # Don't lose the trace just because state fetch
                    # failed. Reward computation will see
                    # ``final_state is None`` and skip db_hash_match.
                    if trace.error is None:
                        trace.error = f"final_state fetch failed: {exc!r}"
            await self._teardown_quiet(session)
            trace.total_duration_seconds = time.monotonic() - t0

        return trace

    async def _loop(
        self,
        trace: TaskTrace,
        session: SessionInfo,
        user: UserSimulator,
        first_user_msg: str,
    ) -> None:
        user_msg = first_user_msg
        recent_user_messages: list[str] = []

        for turn_idx in range(self._max_turns):
            trace.turns.append(TurnRecord(role="user", text=user_msg))

            if _has_stop_token(user_msg):
                trace.stop_reason = "user_stop_token"
                return

            t_turn = time.monotonic()
            try:
                agent_result = await self._backend.submit_turn(
                    session.chatflow_id, user_msg
                )
            except Exception as exc:  # noqa: BLE001
                trace.error = f"submit_turn failed at turn {turn_idx}: {exc!r}"
                trace.stop_reason = "backend_error"
                return
            duration = time.monotonic() - t_turn

            trace.turns.append(
                TurnRecord(
                    role="agent",
                    text=agent_result.agent_response,
                    node_id=agent_result.node_id,
                    duration_seconds=duration,
                )
            )

            if agent_result.status != "succeeded":
                trace.stop_reason = f"agent_status_{agent_result.status}"
                return

            if _has_stop_token(agent_result.agent_response):
                trace.stop_reason = "agent_stop_token"
                return

            try:
                user_msg = user.step(agent_result.agent_response)
            except Exception as exc:  # noqa: BLE001
                trace.error = f"user_simulator.step failed at turn {turn_idx}: {exc!r}"
                trace.stop_reason = "user_simulator_step_error"
                return

            # No-progress detector: if the user simulator emits the same
            # message N times in a row, we're not converging — stop
            # rather than burn token budget on a stuck loop.
            recent_user_messages.append(user_msg)
            if len(recent_user_messages) > self._no_progress_threshold:
                recent_user_messages.pop(0)
            if (
                len(recent_user_messages) == self._no_progress_threshold
                and len(set(recent_user_messages)) == 1
            ):
                trace.stop_reason = "user_no_progress"
                return

        trace.stop_reason = "max_turns"

    async def _teardown_quiet(self, session: SessionInfo) -> None:
        try:
            await self._backend.teardown_session(session.session_id)
        except Exception:  # noqa: BLE001
            # Teardown failure shouldn't mask whatever the run produced.
            # Log via stderr is the runner's responsibility (PR 4 CLI).
            pass
