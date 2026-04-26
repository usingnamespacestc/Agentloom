"""Unit tests for ``TauBenchRunner`` — the multi-turn orchestrator.

Stubs both the backend client and the user simulator so the runner's
loop logic can be exercised without a real backend or LLM. Verifies:

- happy path: 5-turn dialogue terminates on agent stop token
- user simulator emits stop token → terminates immediately
- max_turns cap fires when neither side stops
- no-progress detector catches the user simulator looping on the
  same message
- backend / user_simulator errors get captured in trace.error and
  teardown still happens
- teardown failures don't mask trace
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agentloom_bench.tau_bench.client import SessionInfo, TurnResult
from agentloom_bench.tau_bench.runner import TauBenchRunner


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class StubBackend:
    """Records all calls; returns canned responses set by the test."""

    create_response: SessionInfo = field(
        default_factory=lambda: SessionInfo(
            session_id="sess-1",
            chatflow_id="sess-1",
            domain="retail",
            task_index=0,
            instruction="instr",
            num_tools=16,
        )
    )
    agent_replies: list[str] = field(default_factory=list)
    submit_calls: list[tuple[str, str]] = field(default_factory=list)
    teardown_calls: list[str] = field(default_factory=list)
    submit_should_raise: Exception | None = None
    teardown_should_raise: Exception | None = None

    async def create_session(self, **kwargs):
        return self.create_response

    async def submit_turn(self, chatflow_id, text, **kwargs):
        self.submit_calls.append((chatflow_id, text))
        if self.submit_should_raise is not None:
            raise self.submit_should_raise
        if not self.agent_replies:
            return TurnResult(node_id="n", status="succeeded", agent_response="(echo)")
        reply = self.agent_replies.pop(0)
        # Tests can also force a non-succeeded status by encoding it as
        # "STATUS:<status>:<text>" sentinel.
        if reply.startswith("STATUS:"):
            _, status, body = reply.split(":", 2)
            return TurnResult(node_id="n", status=status, agent_response=body)
        return TurnResult(node_id="n", status="succeeded", agent_response=reply)

    async def teardown_session(self, session_id):
        self.teardown_calls.append(session_id)
        if self.teardown_should_raise is not None:
            raise self.teardown_should_raise
        return {"ok": True, "session_id": session_id, "unregistered_tools": 16}


@dataclass
class StubUser:
    """Pre-canned message stream — returns whatever the test queued."""

    messages: list[str] = field(default_factory=list)
    instructions_seen: list[str] = field(default_factory=list)
    step_inputs: list[str] = field(default_factory=list)

    def reset(self, instruction):
        self.instructions_seen.append(instruction)
        return self.messages.pop(0) if self.messages else "(empty queue)"

    def step(self, content):
        self.step_inputs.append(content)
        return self.messages.pop(0) if self.messages else "(out of queue)"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_5_turn_happy_path_terminates_on_agent_stop():
    backend = StubBackend(
        agent_replies=[
            "agent reply 1",
            "agent reply 2",
            "agent reply 3",
            "agent reply 4",
            "ok done ###STOP###",  # agent emits stop on turn 5
        ]
    )
    user = StubUser(
        messages=["hi", "next1", "next2", "next3", "next4", "should-not-be-read"]
    )
    runner = TauBenchRunner(backend)

    trace = await runner.run_task(
        domain="retail", task_index=0, user_simulator=user
    )

    assert trace.stop_reason == "agent_stop_token"
    assert trace.error is None
    assert backend.teardown_calls == ["sess-1"]
    # 5 user turns + 5 agent turns = 10 records
    assert len(trace.turns) == 10
    assert [r.role for r in trace.turns] == ["user", "agent"] * 5
    assert trace.turns[-1].text.endswith("###STOP###")
    assert trace.instruction == "instr"
    assert trace.session_id == "sess-1"


@pytest.mark.asyncio
async def test_runner_user_stop_token_terminates_immediately():
    backend = StubBackend(agent_replies=["unused"])
    user = StubUser(messages=["I'm satisfied ###STOP###"])
    trace = await TauBenchRunner(backend).run_task(
        domain="retail", task_index=0, user_simulator=user
    )
    assert trace.stop_reason == "user_stop_token"
    # User msg recorded; no submit_turn fired since stop fires before send
    assert len(trace.turns) == 1
    assert backend.submit_calls == []
    assert backend.teardown_calls == ["sess-1"]


@pytest.mark.asyncio
async def test_runner_max_turns_cap():
    backend = StubBackend(agent_replies=["a"] * 50)
    # User just keeps pinging
    user = StubUser(messages=[f"msg-{i}" for i in range(50)])
    runner = TauBenchRunner(backend, max_turns=3, no_progress_threshold=10)
    trace = await runner.run_task(
        domain="retail", task_index=0, user_simulator=user
    )
    assert trace.stop_reason == "max_turns"
    # 3 user + 3 agent turns
    assert len(trace.turns) == 6
    assert backend.teardown_calls == ["sess-1"]


@pytest.mark.asyncio
async def test_runner_no_progress_detector():
    backend = StubBackend(agent_replies=["reply"] * 20)
    # User loops the same message, threshold 3
    user = StubUser(messages=["ping"] * 20)
    runner = TauBenchRunner(backend, max_turns=20, no_progress_threshold=3)
    trace = await runner.run_task(
        domain="retail", task_index=0, user_simulator=user
    )
    assert trace.stop_reason == "user_no_progress"
    # 4 user turns (1 from reset + 3 from steps) — same msg detected after
    # the 3rd repeat, so loop breaks before submitting the 4th turn.
    assert len([r for r in trace.turns if r.role == "user"]) <= 4


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_backend_error_captured_and_torn_down():
    class BoomError(RuntimeError):
        pass

    backend = StubBackend(submit_should_raise=BoomError("boom"))
    user = StubUser(messages=["hi"])
    trace = await TauBenchRunner(backend).run_task(
        domain="retail", task_index=0, user_simulator=user
    )
    assert trace.stop_reason == "backend_error"
    assert "BoomError" in (trace.error or "")
    assert backend.teardown_calls == ["sess-1"]


@pytest.mark.asyncio
async def test_runner_user_step_error_captured():
    backend = StubBackend(agent_replies=["reply"])

    class ExplodingUser:
        def reset(self, instruction):
            return "first"

        def step(self, content):
            raise ValueError("user simulator died")

    trace = await TauBenchRunner(backend).run_task(
        domain="retail", task_index=0, user_simulator=ExplodingUser()
    )
    assert trace.stop_reason == "user_simulator_step_error"
    assert "user simulator died" in (trace.error or "")
    assert backend.teardown_calls == ["sess-1"]


@pytest.mark.asyncio
async def test_runner_user_reset_error_skips_loop_but_still_tears_down():
    backend = StubBackend()

    class ResetExploder:
        def reset(self, instruction):
            raise RuntimeError("reset boom")

        def step(self, content):
            return ""

    trace = await TauBenchRunner(backend).run_task(
        domain="retail", task_index=0, user_simulator=ResetExploder()
    )
    assert trace.stop_reason == "user_simulator_reset_error"
    assert "reset boom" in (trace.error or "")
    assert backend.teardown_calls == ["sess-1"]
    assert backend.submit_calls == []


@pytest.mark.asyncio
async def test_runner_teardown_failure_does_not_mask_trace():
    backend = StubBackend(
        agent_replies=["reply ###STOP###"],
        teardown_should_raise=RuntimeError("teardown boom"),
    )
    user = StubUser(messages=["hi"])
    trace = await TauBenchRunner(backend).run_task(
        domain="retail", task_index=0, user_simulator=user
    )
    # Trace still complete — agent stop token recorded
    assert trace.stop_reason == "agent_stop_token"
    # teardown was attempted
    assert backend.teardown_calls == ["sess-1"]


@pytest.mark.asyncio
async def test_runner_agent_failed_status_terminates():
    backend = StubBackend(agent_replies=["STATUS:failed:something broke"])
    user = StubUser(messages=["please do thing"])
    trace = await TauBenchRunner(backend).run_task(
        domain="retail", task_index=0, user_simulator=user
    )
    assert trace.stop_reason == "agent_status_failed"
    assert backend.teardown_calls == ["sess-1"]
