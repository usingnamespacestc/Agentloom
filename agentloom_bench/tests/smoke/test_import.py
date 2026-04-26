"""Smoke import test for the runner conda env.

PR 1 verifies:
1. ``agentloom_bench`` package is importable
2. Upstream ``tau_bench`` (with full agents/ + user.py + litellm chain) is
   importable in this env — proves the git+ install pin worked
3. ``UserStrategy`` (the LLM-driven simulated user) is importable, since
   that's what the runner will call in PR 3+ to drive multi-turn dialogue
4. The same retail / airline tasks_test that backend sees via vendor are
   visible from upstream too — sanity that the pin matches our vendor sha
"""
from __future__ import annotations


def test_agentloom_bench_importable():
    import agentloom_bench

    assert agentloom_bench.__version__


def test_upstream_tau_bench_importable_with_litellm():
    """Runner env MUST have litellm + full tau_bench (not vendor subset).
    If this fails the conda env wasn't set up per agentloom_bench/README.md."""
    import litellm  # noqa: F401 — must be installed in runner env
    import tau_bench
    from tau_bench.envs.user import UserStrategy

    # Sanity that we got upstream, not a vendor leak: vendor doesn't ship
    # tau_bench/agents/, so this import succeeds only on full upstream.
    from tau_bench.agents.base import Agent  # noqa: F401

    # File path should NOT contain backend/vendor/tau_bench
    assert "backend/vendor/tau_bench" not in tau_bench.__file__, (
        f"runner env is shadowing upstream with the backend vendor copy: "
        f"{tau_bench.__file__}"
    )

    assert UserStrategy.LLM


def test_upstream_retail_tasks_match_pinned_sha():
    """The runner's tau_bench must be pinned to the same sha that the
    backend vendor was cherry-picked from — otherwise ground-truth
    reward computation could drift from what the backend tools simulate."""
    from tau_bench.envs.retail.tasks_test import TASKS_TEST as RETAIL_TEST
    from tau_bench.envs.airline.tasks_test import TASKS as AIRLINE_TEST

    # Counts at the pinned sha (59a200c) — guard against silent upstream drift.
    assert len(RETAIL_TEST) == 115, (
        f"retail tasks_test count drifted: got {len(RETAIL_TEST)}, "
        f"expected 115 at sha 59a200c"
    )
    assert len(AIRLINE_TEST) == 50, (
        f"airline tasks count drifted: got {len(AIRLINE_TEST)}, "
        f"expected 50 at sha 59a200c"
    )
