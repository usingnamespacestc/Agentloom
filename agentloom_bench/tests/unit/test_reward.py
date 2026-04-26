"""Unit tests for reward computation.

These exercise the upstream ``tau_bench.envs`` import path with
``user_strategy="human"`` which bypasses litellm. The agentloom
backend env's vendored ``tau_bench`` works the same way thanks to
the patch in ``backend/vendor/tau_bench/PATCHES.md``.

In the agentloom-bench env (with full upstream tau_bench installed)
these tests still pass — the import resolves to the upstream copy
which has identical behavior at the Env level.
"""
from __future__ import annotations

import pytest


def _import_or_skip():
    """Skip if upstream tau_bench is not on PYTHONPATH."""
    try:
        import tau_bench  # noqa: F401
    except ImportError:
        pytest.skip("tau_bench not importable in this env")


def test_reward_no_outputs_task_unchanged_db_yields_zero():
    """Retail task 0 has empty ``outputs`` (it's pure DB-mutation),
    so reward depends entirely on the DB hash check. Pass a fresh
    (unchanged) DB → ground-truth replay produces a different hash
    → reward 0. ``outputs_match`` is vacuously True since nothing
    was required."""
    _import_or_skip()
    from agentloom_bench.tau_bench.reward import compute_reward
    from tau_bench.envs.retail.data import load_data

    breakdown = compute_reward(
        domain="retail",
        task_index=0,
        final_db_state=load_data(),
        agent_responses=[],
    )
    assert breakdown.reward == 0.0
    assert breakdown.db_hash_match is False
    # No outputs declared → vacuously match, no per-string entries
    assert breakdown.outputs_match is True
    assert breakdown.outputs_per_string == {}


def test_reward_outputs_match_when_response_contains_string():
    """Retail task 2 has ``outputs=['10']``. If agent reply contains
    "10", outputs_match flips True. DB still won't match since we
    pass fresh data."""
    _import_or_skip()
    from agentloom_bench.tau_bench.reward import compute_reward
    from tau_bench.envs.retail.data import load_data
    from tau_bench.envs.retail.tasks_test import TASKS_TEST

    task = TASKS_TEST[2]
    assert task.outputs, "test pinned to task 2 which should have outputs"

    fake_response = " | ".join(task.outputs)
    breakdown = compute_reward(
        domain="retail",
        task_index=2,
        final_db_state=load_data(),
        agent_responses=[fake_response],
    )
    assert breakdown.outputs_match is True
    assert all(v is True for v in breakdown.outputs_per_string.values())
    # DB hash mismatch → reward still 0 even though outputs matched
    assert breakdown.db_hash_match is False
    assert breakdown.reward == 0.0


def test_reward_outputs_miss_when_response_does_not_contain_string():
    """Same retail task 2, but agent responses don't contain '10' →
    outputs_match flips False, per-string map shows it missing."""
    _import_or_skip()
    from agentloom_bench.tau_bench.reward import compute_reward
    from tau_bench.envs.retail.data import load_data

    breakdown = compute_reward(
        domain="retail",
        task_index=2,
        final_db_state=load_data(),
        agent_responses=["nothing relevant", "still nothing"],
    )
    assert breakdown.outputs_match is False
    assert any(v is False for v in breakdown.outputs_per_string.values())


def test_reward_db_match_when_data_matches_groundtruth_replay():
    """If we replay the ground-truth task.actions onto a fresh DB
    ourselves and pass that as final_db_state, db_hash_match should
    be True. Combined with outputs match → reward 1.0."""
    _import_or_skip()
    from agentloom_bench.tau_bench.reward import compute_reward
    from tau_bench.envs import get_env

    # Build a temporary env to replay ground truth onto fresh data
    env = get_env(
        env_name="retail",
        user_strategy="human",
        user_model="placeholder",
        task_split="test",
        task_index=0,
    )
    # fresh data + replay
    env.data = env.data_load_func()
    for action in env.task.actions:
        if action.name not in env.terminate_tools:
            env.step(action)
    replayed = env.data
    fake_response = " | ".join(env.task.outputs) if env.task.outputs else ""

    breakdown = compute_reward(
        domain="retail",
        task_index=0,
        final_db_state=replayed,
        agent_responses=[fake_response],
    )
    assert breakdown.db_hash_match is True
    assert breakdown.outputs_match is True
    assert breakdown.reward == 1.0
