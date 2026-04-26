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


# ---------------------------------------------------------------------------
# Batch aggregation
# ---------------------------------------------------------------------------


def _classify_failure(record: dict[str, Any]) -> str:
    """Heuristic per-task failure category. Used for the batch
    summary's distribution table.

    Categories:
    - ``success`` — reward == 1.0
    - ``no_reward`` — reward column never populated (run errored before
      reward computation, or --skip-reward was set)
    - ``db_hash_mismatch`` — reward 0 because final DB diverged from
      ground-truth replay (agent didn't make the right tool calls)
    - ``outputs_miss`` — reward 0 because declared output strings
      weren't found in any agent reply (agent answered but missed
      required content)
    - ``user_no_progress`` — runner detector saw user looping
    - ``max_turns`` — hit the cap without either side stopping
    - ``backend_error`` — runner stop_reason == ``backend_error`` or
      trace.error populated
    - ``user_simulator_error`` — user.reset / user.step exploded
    - ``other`` — anything else (e.g. agent_stop with reward 0 + no
      DB / outputs mismatch — shouldn't happen but keeps the bucket
      list closed)
    """
    reward = record.get("reward")
    breakdown = record.get("reward_breakdown") or {}
    stop_reason = record.get("stop_reason", "")
    error = record.get("error")

    if reward == 1.0:
        return "success"
    if reward is None:
        return "no_reward"
    if stop_reason == "backend_error" or (error and "backend" in error.lower()):
        return "backend_error"
    if stop_reason in {"user_simulator_reset_error", "user_simulator_step_error"}:
        return "user_simulator_error"
    if stop_reason == "user_no_progress":
        return "user_no_progress"
    if stop_reason == "max_turns":
        return "max_turns"
    if breakdown.get("db_hash_match") is False:
        return "db_hash_mismatch"
    if breakdown.get("outputs_match") is False:
        return "outputs_miss"
    return "other"


def aggregate_batch_to_markdown(
    out_dir: Path,
    *,
    task_indices: list[int] | None = None,
    title_extra: str = "",
) -> str:
    """Read every ``task_*.json`` in ``out_dir`` (or only the listed
    ``task_indices`` if provided) and render a markdown summary.

    Returns the markdown text. Caller decides where to write it
    (``write_batch_report`` for ``out_dir/summary.md``; report-history
    paths in ``docs/reports/`` for archival).
    """
    records: list[dict[str, Any]] = []
    missing: list[int] = []

    if task_indices is None:
        # Discover from filesystem
        files = sorted(out_dir.glob("task_*.json"))
        for f in files:
            with f.open("r", encoding="utf-8") as fp:
                records.append(json.load(fp))
    else:
        for i in task_indices:
            f = out_dir / f"task_{i}.json"
            if not f.exists():
                missing.append(i)
                continue
            with f.open("r", encoding="utf-8") as fp:
                records.append(json.load(fp))

    n = len(records)
    if n == 0:
        return f"# τ-bench batch summary\n\n_no per-task records found in `{out_dir}`._\n"

    domain = records[0].get("domain", "?")
    agent_model = records[0].get("agent_model", "?")
    user_model = records[0].get("user_model", "?")

    n_success = sum(1 for r in records if r.get("reward") == 1.0)
    pass_rate = n_success / n

    # Category distribution
    counts: dict[str, int] = {}
    for r in records:
        cat = _classify_failure(r)
        counts[cat] = counts.get(cat, 0) + 1

    # Per-task table
    rows = []
    for r in sorted(records, key=lambda r: r.get("task_idx", -1)):
        idx = r.get("task_idx", "?")
        reward = r.get("reward")
        reward_str = f"{reward}" if reward is not None else "—"
        stop = r.get("stop_reason", "?")
        n_turns = len(r.get("turns") or [])
        # tool call count: count agent turns that have non-empty tool history
        # (we don't currently surface tool_uses from the engine via the
        # /turns response, so this is an approximation: count agent turns).
        n_agent_turns = sum(
            1 for t in (r.get("turns") or []) if t.get("role") == "agent"
        )
        dur = r.get("total_duration_seconds")
        dur_str = f"{dur:.1f}s" if isinstance(dur, (int, float)) else "?"
        cat = _classify_failure(r)
        rows.append(
            f"| {idx} | {reward_str} | {cat} | {stop} | {n_turns} | {n_agent_turns} | {dur_str} |"
        )

    # Duration stats
    durs = [r.get("total_duration_seconds") for r in records if isinstance(r.get("total_duration_seconds"), (int, float))]
    durs_sorted = sorted(durs)

    def _percentile(p: float) -> float | None:
        if not durs_sorted:
            return None
        k = int(len(durs_sorted) * p)
        k = min(k, len(durs_sorted) - 1)
        return durs_sorted[k]

    p50 = _percentile(0.5)
    p95 = _percentile(0.95)
    total_dur = sum(durs) if durs else 0.0

    # Build markdown
    title = f"# τ-bench {domain} batch summary"
    if title_extra:
        title += f" — {title_extra}"

    md_lines: list[str] = []
    md_lines.append(title)
    md_lines.append("")
    md_lines.append(f"- **agent_model**: `{agent_model}`")
    md_lines.append(f"- **user_model**: `{user_model}`")
    md_lines.append(f"- **tasks**: {n} ({n_success} pass, {n - n_success} fail)")
    md_lines.append(f"- **pass^1 rate**: {pass_rate:.1%}")
    if missing:
        md_lines.append(f"- **missing per-task JSONs**: {missing}")
    md_lines.append(
        f"- **total wall time**: {total_dur:.0f}s "
        f"(p50 {p50:.0f}s, p95 {p95:.0f}s)"
        if p50 is not None and p95 is not None
        else f"- **total wall time**: {total_dur:.0f}s"
    )
    md_lines.append("")

    md_lines.append("## Failure category distribution")
    md_lines.append("")
    md_lines.append("| Category | Count |")
    md_lines.append("|---|---|")
    for cat in sorted(counts.keys(), key=lambda c: -counts[c]):
        md_lines.append(f"| {cat} | {counts[cat]} |")
    md_lines.append("")

    md_lines.append("## Per-task results")
    md_lines.append("")
    md_lines.append(
        "| Task | Reward | Category | Stop reason | Turns | Agent turns | Duration |"
    )
    md_lines.append("|---|---|---|---|---|---|---|")
    md_lines.extend(rows)
    md_lines.append("")

    md_lines.append("## Notes")
    md_lines.append("")
    md_lines.append(
        "- **agent turns** is a proxy for tool-loop richness; the engine "
        "doesn't currently surface inner WorkNode tool_use counts via the "
        "`/turns` response, so PR 6+ should add that for finer attribution."
    )
    md_lines.append(
        "- **categories** use heuristic classification "
        "(`tau_bench/report.py::_classify_failure`); inspect individual "
        "`task_*.json` files for full agent/user transcripts when a "
        "category looks suspicious."
    )

    return "\n".join(md_lines) + "\n"


def write_batch_report(
    *,
    out_dir: Path,
    domain: str,  # noqa: ARG001 — included in markdown via per-task records
    agent_model: str,  # noqa: ARG001 — same
    user_model: str,  # noqa: ARG001 — same
    task_indices: list[int],
    filename: str = "summary.md",
) -> Path:
    """Write aggregated markdown to ``out_dir/{filename}``. Returns
    the written path. Records inside ``out_dir`` (per-task JSONs)
    are the source of truth; arguments here are present so callers
    can override defaults if needed."""
    out_dir.mkdir(parents=True, exist_ok=True)
    md = aggregate_batch_to_markdown(out_dir, task_indices=task_indices)
    path = out_dir / filename
    path.write_text(md, encoding="utf-8")
    return path
