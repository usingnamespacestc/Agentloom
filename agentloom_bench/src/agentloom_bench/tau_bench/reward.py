"""Reward computation for τ-bench tasks.

Runner-side reward calc: take the mutated mock DB pulled from the
backend + the agent's response messages from the task trace, plug
them into a fresh upstream ``Env``, and call ``Env.calculate_reward()``
which:

1. Hashes our final ``self.data`` (the backend's mutations replayed)
2. Resets ``self.data`` to a clean load, then replays
   ``task.actions`` (ground truth) and hashes again
3. Compares the two hashes (``r_actions``)
4. Scans ``self.actions`` (RESPOND-shaped agent messages) for
   ``task.outputs`` substring match (``r_outputs``)
5. ``reward = 1.0`` only if both pass

Backend stays minimal — it just exposes the data dict via
``GET /sessions/{id}/state`` (PR 4 endpoint). Reward logic stays in
the runner where the upstream ``Env`` class is fully installed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RewardBreakdown:
    """Decomposed reward result. ``reward`` is the standard τ-bench
    1/0 score; the breakdown lets users see why a task failed."""

    reward: float
    db_hash_match: bool
    outputs_match: bool
    outputs_per_string: dict[str, bool]


def compute_reward(
    *,
    domain: str,
    task_index: int,
    final_db_state: dict[str, Any],
    agent_responses: list[str],
) -> RewardBreakdown:
    """Compute τ-bench reward locally.

    Constructs a fresh upstream ``Env`` (runner conda env has full
    ``tau_bench`` + ``litellm`` installed, vs backend's vendored
    subset). Computes db_hash_match directly via ``env.get_data_hash()``
    (so we can report db match status independent of upstream
    ``calculate_reward``'s reward=0 short-circuit), then delegates
    outputs check via the standard call.

    The user simulator parameter is set to ``"human"`` so we don't
    accidentally hit any LLM during init — reward computation is
    pure data comparison; user persona is irrelevant.
    """
    from tau_bench.envs import get_env
    from tau_bench.types import Action

    env = get_env(
        env_name=domain,
        user_strategy="human",
        user_model="placeholder",
        task_split="test",
        task_index=task_index,
    )

    # --- Stage 1: db_hash_match (computed directly so we get a
    # truthful flag regardless of upstream's short-circuit logic).
    env.data = final_db_state
    candidate_hash = env.get_data_hash()
    env.data = env.data_load_func()  # reset
    for action in env.task.actions:
        if action.name not in env.terminate_tools:
            env.step(action)
    gt_hash = env.get_data_hash()
    db_hash_match = candidate_hash == gt_hash

    # --- Stage 2: outputs_match. Re-set env.data because step calls
    # above mutated it; populate env.actions with agent RESPOND turns.
    env.data = final_db_state
    env.actions = [
        Action(name="respond", kwargs={"content": resp}) for resp in agent_responses
    ]

    outputs_per_string: dict[str, bool] = {}
    if env.task.outputs:
        for output in env.task.outputs:
            found = False
            for action in env.actions:
                if action.name == "respond" and (
                    output.lower() in action.kwargs["content"].lower().replace(",", "")
                ):
                    found = True
                    break
            outputs_per_string[output] = found
        outputs_match = all(outputs_per_string.values())
    else:
        outputs_match = True  # vacuously match when nothing declared

    reward = 1.0 if (db_hash_match and outputs_match) else 0.0
    return RewardBreakdown(
        reward=reward,
        db_hash_match=db_hash_match,
        outputs_match=outputs_match,
        outputs_per_string=outputs_per_string,
    )
