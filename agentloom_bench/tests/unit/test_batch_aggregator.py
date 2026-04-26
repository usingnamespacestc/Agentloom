"""Unit tests for the batch markdown aggregator."""
from __future__ import annotations

import json

import pytest

from agentloom_bench.tau_bench.report import (
    _classify_failure,
    aggregate_batch_to_markdown,
    write_batch_report,
)


def _record(
    task_idx: int = 0,
    reward: float | None = 1.0,
    db_hash_match: bool = True,
    outputs_match: bool = True,
    stop_reason: str = "user_stop_token",
    error: str | None = None,
    turns: int = 8,
    agent_turns: int = 4,
    duration: float = 200.0,
    domain: str = "retail",
) -> dict:
    breakdown = (
        {
            "db_hash_match": db_hash_match,
            "outputs_match": outputs_match,
            "outputs_per_string": {},
        }
        if reward is not None
        else None
    )
    turn_records = []
    for i in range(turns):
        role = "agent" if i % 2 == 1 else "user"
        turn_records.append({"role": role, "text": f"t{i}"})
    return {
        "task_idx": task_idx,
        "domain": domain,
        "agent_model": "p:m",
        "user_model": "anthropic:claude-haiku",
        "user_strategy": "llm",
        "instruction": "...",
        "session_id": f"s{task_idx}",
        "chatflow_id": f"cf{task_idx}",
        "turns": turn_records,
        "stop_reason": stop_reason,
        "error": error,
        "total_duration_seconds": duration,
        "reward": reward,
        "reward_breakdown": breakdown,
    }


# ---------------------------------------------------------------------------
# _classify_failure
# ---------------------------------------------------------------------------


def test_classify_success():
    assert _classify_failure(_record(reward=1.0)) == "success"


def test_classify_no_reward():
    assert _classify_failure(_record(reward=None)) == "no_reward"


def test_classify_db_hash_mismatch():
    rec = _record(reward=0.0, db_hash_match=False, outputs_match=True)
    assert _classify_failure(rec) == "db_hash_mismatch"


def test_classify_outputs_miss():
    rec = _record(reward=0.0, db_hash_match=True, outputs_match=False)
    assert _classify_failure(rec) == "outputs_miss"


def test_classify_user_no_progress():
    rec = _record(reward=0.0, stop_reason="user_no_progress")
    assert _classify_failure(rec) == "user_no_progress"


def test_classify_max_turns():
    rec = _record(reward=0.0, stop_reason="max_turns")
    assert _classify_failure(rec) == "max_turns"


def test_classify_backend_error():
    rec = _record(reward=0.0, stop_reason="backend_error")
    assert _classify_failure(rec) == "backend_error"


def test_classify_user_simulator_error():
    rec = _record(reward=0.0, stop_reason="user_simulator_step_error")
    assert _classify_failure(rec) == "user_simulator_error"


# ---------------------------------------------------------------------------
# aggregate_batch_to_markdown
# ---------------------------------------------------------------------------


def test_aggregate_empty_dir_produces_no_records_message(tmp_path):
    md = aggregate_batch_to_markdown(tmp_path)
    assert "no per-task records" in md


def test_aggregate_simple_pass_rate(tmp_path):
    # 3 successes, 2 failures → pass^1 = 0.6
    for i in range(3):
        path = tmp_path / f"task_{i}.json"
        path.write_text(json.dumps(_record(task_idx=i, reward=1.0)))
    for i in (3, 4):
        path = tmp_path / f"task_{i}.json"
        path.write_text(
            json.dumps(_record(task_idx=i, reward=0.0, db_hash_match=False))
        )

    md = aggregate_batch_to_markdown(tmp_path, task_indices=list(range(5)))
    assert "5 (3 pass, 2 fail)" in md
    assert "60.0%" in md
    assert "| success | 3 |" in md
    assert "| db_hash_mismatch | 2 |" in md


def test_aggregate_missing_files_listed(tmp_path):
    # Only task 0 + 2 written; 1 missing
    (tmp_path / "task_0.json").write_text(json.dumps(_record(task_idx=0)))
    (tmp_path / "task_2.json").write_text(json.dumps(_record(task_idx=2)))
    md = aggregate_batch_to_markdown(tmp_path, task_indices=[0, 1, 2])
    assert "missing per-task JSONs" in md
    assert "[1]" in md


def test_aggregate_per_task_table_sorted(tmp_path):
    # Write out of order; aggregator should sort by task_idx in table
    for i in [3, 1, 0, 2]:
        (tmp_path / f"task_{i}.json").write_text(json.dumps(_record(task_idx=i)))
    md = aggregate_batch_to_markdown(tmp_path, task_indices=[0, 1, 2, 3])
    # The Per-task results table rows in markdown order should be 0,1,2,3
    pos = [md.index(f"| {i} |") for i in [0, 1, 2, 3]]
    assert pos == sorted(pos)


def test_write_batch_report_creates_summary_md(tmp_path):
    (tmp_path / "task_0.json").write_text(json.dumps(_record(task_idx=0)))
    path = write_batch_report(
        out_dir=tmp_path,
        domain="retail",
        agent_model="p:m",
        user_model="u:m",
        task_indices=[0],
    )
    assert path.name == "summary.md"
    md = path.read_text()
    assert "agent_model" in md
    assert "Per-task results" in md
