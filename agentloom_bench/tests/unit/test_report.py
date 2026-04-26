"""Unit tests for report writer + serializer."""
from __future__ import annotations

import json

from agentloom_bench.tau_bench.report import serialize_task_run, write_task_report
from agentloom_bench.tau_bench.reward import RewardBreakdown
from agentloom_bench.tau_bench.runner import TaskTrace, TurnRecord


def _trace(reward: bool = True) -> TaskTrace:
    return TaskTrace(
        domain="retail",
        task_index=0,
        session_id="sess-1",
        chatflow_id="sess-1",
        instruction="do thing",
        turns=[
            TurnRecord(role="user", text="hi"),
            TurnRecord(role="agent", text="hello", node_id="n1", duration_seconds=2.5),
        ],
        stop_reason="agent_stop_token",
        total_duration_seconds=3.0,
        final_state={"orders": {}},
    )


def test_serialize_with_reward_includes_breakdown():
    trace = _trace()
    reward = RewardBreakdown(
        reward=1.0,
        db_hash_match=True,
        outputs_match=True,
        outputs_per_string={"foo": True},
    )
    payload = serialize_task_run(
        trace=trace,
        agent_model="prov:m",
        user_model="prov:m",
        user_strategy="llm",
        reward=reward,
    )
    assert payload["task_idx"] == 0
    assert payload["domain"] == "retail"
    assert payload["agent_model"] == "prov:m"
    assert payload["reward"] == 1.0
    assert payload["reward_breakdown"]["db_hash_match"] is True
    assert payload["reward_breakdown"]["outputs_per_string"] == {"foo": True}
    assert len(payload["turns"]) == 2
    assert payload["turns"][0]["role"] == "user"
    assert payload["turns"][1]["duration_seconds"] == 2.5


def test_serialize_without_reward_sets_null():
    payload = serialize_task_run(
        trace=_trace(),
        agent_model="m",
        user_model="m",
        user_strategy="llm",
        reward=None,
    )
    assert payload["reward"] is None
    assert payload["reward_breakdown"] is None


def test_write_task_report_creates_file_at_expected_path(tmp_path):
    out = tmp_path / "runs" / "x"
    path = write_task_report(
        out_dir=out,
        trace=_trace(),
        agent_model="m",
        user_model="m",
        user_strategy="llm",
        reward=None,
    )
    assert path.exists()
    assert path.name == "task_0.json"
    parsed = json.loads(path.read_text())
    assert parsed["task_idx"] == 0
