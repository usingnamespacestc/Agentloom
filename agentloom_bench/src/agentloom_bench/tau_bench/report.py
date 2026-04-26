"""Per-task JSON report writer.

Schema (writes one file per task to ``out_dir/task_{idx}.json``):

```json
{
    "task_idx": 0,
    "domain": "retail",
    "agent_model": "ark-code-latest",
    "user_model": "ark-code-latest",
    "user_strategy": "llm",
    "instruction": "...",
    "session_id": "...",
    "chatflow_id": "...",
    "turns": [{"role": "user", "text": "..."},
              {"role": "agent", "text": "...", "node_id": "...", "duration_seconds": 1.2}],
    "stop_reason": "agent_stop_token | user_stop_token | max_turns | ...",
    "error": null,
    "total_duration_seconds": 281.4,
    "reward": 1.0,
    "reward_breakdown": {
        "db_hash_match": true,
        "outputs_match": true,
        "outputs_per_string": {"required-string-A": true}
    }
}
```
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .reward import RewardBreakdown
from .runner import TaskTrace


def serialize_task_run(
    *,
    trace: TaskTrace,
    agent_model: str,
    user_model: str,
    user_strategy: str,
    reward: RewardBreakdown | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_idx": trace.task_index,
        "domain": trace.domain,
        "agent_model": agent_model,
        "user_model": user_model,
        "user_strategy": user_strategy,
        "instruction": trace.instruction,
        "session_id": trace.session_id,
        "chatflow_id": trace.chatflow_id,
        "turns": [asdict(t) for t in trace.turns],
        "stop_reason": trace.stop_reason,
        "error": trace.error,
        "total_duration_seconds": trace.total_duration_seconds,
    }
    if reward is not None:
        payload["reward"] = reward.reward
        payload["reward_breakdown"] = {
            "db_hash_match": reward.db_hash_match,
            "outputs_match": reward.outputs_match,
            "outputs_per_string": reward.outputs_per_string,
        }
    else:
        payload["reward"] = None
        payload["reward_breakdown"] = None
    return payload


def write_task_report(
    *,
    out_dir: Path,
    trace: TaskTrace,
    agent_model: str,
    user_model: str,
    user_strategy: str,
    reward: RewardBreakdown | None,
) -> Path:
    """Write the task's JSON to ``out_dir/task_{idx}.json``. Creates
    ``out_dir`` if missing. Returns the written path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"task_{trace.task_index}.json"
    payload = serialize_task_run(
        trace=trace,
        agent_model=agent_model,
        user_model=user_model,
        user_strategy=user_strategy,
        reward=reward,
    )
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path
