"""``agentloom-bench`` CLI — driver for τ-bench tasks against a
running Agentloom backend.

PR 5: batch mode. ``--task-ids`` accepts ``"0"`` (single),
``"0-9"`` (range, inclusive), ``"0,1,5"`` (explicit list), or
combinations like ``"0-2,5,7-9"``. After the batch finishes, an
aggregated markdown report is written next to the per-task JSONs.

Example (single task):

    agentloom-bench \\
        --domain retail --task-ids 0 \\
        --backend-url http://localhost:8000 \\
        --agent-provider <provider-id> --agent-model ark-code-latest \\
        --user-provider anthropic --user-model claude-haiku-4-5 \\
        --max-turns 30 --out runs/latest

Example (batch retail 0-9):

    agentloom-bench \\
        --domain retail --task-ids 0-9 \\
        --backend-url http://localhost:8000 \\
        --agent-provider <provider-id> --agent-model ark-code-latest \\
        --user-provider anthropic --user-model claude-haiku-4-5 \\
        --max-turns 30 --out runs/2026-04-26-retail-0to9
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

import httpx
import typer

from .tau_bench.adapter import build_user_simulator
from .tau_bench.client import TauBenchBackendClient
from .tau_bench.report import (
    aggregate_batch_to_markdown,
    write_batch_report,
    write_task_report,
)
from .tau_bench.reward import RewardBreakdown, compute_reward
from .tau_bench.runner import TauBenchRunner

app = typer.Typer(no_args_is_help=True)


def _parse_task_ids(spec: str) -> list[int]:
    """Parse ``--task-ids`` spec strings.

    Accepts: ``"0"`` (single), ``"0-9"`` (inclusive range), ``"0,1,5"``
    (explicit list), or combinations like ``"0-2,5,7-9"``. Whitespace
    around delimiters is tolerated.
    """
    out: list[int] = []
    seen: set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start_s, end_s = chunk.split("-", 1)
            start, end = int(start_s.strip()), int(end_s.strip())
            if end < start:
                raise typer.BadParameter(
                    f"task range '{chunk}' has end < start"
                )
            for i in range(start, end + 1):
                if i not in seen:
                    out.append(i)
                    seen.add(i)
        else:
            i = int(chunk)
            if i not in seen:
                out.append(i)
                seen.add(i)
    if not out:
        raise typer.BadParameter("--task-ids must contain at least one index")
    return out


@app.command()
def run(
    domain: str = typer.Option(..., "--domain", help="``retail`` or ``airline``"),
    task_ids: str = typer.Option(
        "0",
        "--task-ids",
        help="Task index spec: ``0``, ``0-9``, ``0,1,5``, or ``0-2,5,7-9``.",
    ),
    backend_url: str = typer.Option(
        "http://localhost:8000",
        "--backend-url",
        help="Agentloom backend base URL.",
    ),
    agent_provider: str = typer.Option(
        ..., "--agent-provider", help="Provider id for the agent's model."
    ),
    agent_model: str = typer.Option(
        ..., "--agent-model", help="Model id under that provider."
    ),
    user_model: str = typer.Option(
        "doubao-seed-2-0-pro-260215",
        "--user-model",
        help="Model id for the user simulator (litellm-style). Default "
        "is volcengine doubao-seed 2.0 Pro (latest general-purpose "
        "Pro tier; free tier on volcengine API). Versionless aliases "
        "like ``doubao-seed-2-0-pro`` are NOT accepted — the date "
        "suffix is required. The ARK console alias ``ark-code-latest`` "
        "is also rejected by litellm; pass actual doubao series ids.",
    ),
    user_provider: Optional[str] = typer.Option(
        "volcengine",
        "--user-provider",
        help="litellm provider prefix for user simulator. Default "
        "``volcengine`` (free tier with VOLCENGINE_API_KEY). Override "
        "to ``anthropic`` etc. when running paid baselines.",
    ),
    user_strategy: str = typer.Option(
        "llm",
        "--user-strategy",
        help="One of llm / react / verify / reflection / human.",
    ),
    max_turns: int = typer.Option(30, "--max-turns"),
    out: Path = typer.Option(
        Path("runs/latest"),
        "--out",
        help="Directory to write per-task JSON reports + aggregated markdown.",
    ),
    skip_reward: bool = typer.Option(
        False,
        "--skip-reward",
        help="Skip reward computation (for plumbing-only smoke runs).",
    ),
    skip_aggregate: bool = typer.Option(
        False,
        "--skip-aggregate",
        help="Skip writing the aggregated markdown report at the end.",
    ),
    execution_mode: Optional[str] = typer.Option(
        None,
        "--execution-mode",
        help="ChatFlow execution mode for the spawned session. ``None`` "
        "(default) uses the backend default (native_react). Pass "
        "``auto_plan`` to exercise the full cognitive pipeline; "
        "``semi_auto`` for plan + judges without auto-decompose.",
    ),
) -> None:
    """Run one or more τ-bench tasks against the Agentloom backend."""
    indices = _parse_task_ids(task_ids)
    asyncio.run(
        _run_batch(
            domain=domain,
            task_indices=indices,
            backend_url=backend_url,
            agent_provider=agent_provider,
            agent_model=agent_model,
            user_model=user_model,
            user_provider=user_provider,
            user_strategy=user_strategy,
            max_turns=max_turns,
            out=out,
            skip_reward=skip_reward,
            skip_aggregate=skip_aggregate,
            execution_mode=execution_mode,
        )
    )


async def _run_batch(
    *,
    domain: str,
    task_indices: list[int],
    backend_url: str,
    agent_provider: str,
    agent_model: str,
    user_model: str,
    user_provider: Optional[str],
    user_strategy: str,
    max_turns: int,
    out: Path,
    skip_reward: bool,
    skip_aggregate: bool,
    execution_mode: Optional[str] = None,
) -> None:
    n = len(task_indices)
    typer.echo(
        f"agentloom-bench: domain={domain} tasks={task_indices} "
        f"agent={agent_provider}:{agent_model} user={user_provider}:{user_model}"
    )
    async with httpx.AsyncClient(base_url=backend_url) as http:
        backend = TauBenchBackendClient(http)
        runner = TauBenchRunner(backend, max_turns=max_turns)
        # Sequential to avoid concurrent submit_turn FrozenNode race
        # (per-cf-id submit_lock fixed same-chatflow concurrency in
        # commit a056243; different chatflows can in principle run in
        # parallel but rate limits + simpler debugging keep us serial).
        for i, task_id in enumerate(task_indices, start=1):
            typer.echo(f"[{i}/{n}] task_id={task_id} starting...")
            # Each task gets a fresh user simulator instance so internal
            # state (turn counter, history) doesn't leak between tasks.
            try:
                user = build_user_simulator(
                    user_model=user_model,
                    user_strategy=user_strategy,
                    user_provider=user_provider,
                )
            except Exception as exc:  # noqa: BLE001
                typer.echo(
                    f"  user simulator init failed for task {task_id}: {exc!r}"
                )
                continue

            try:
                trace = await runner.run_task(
                    domain=domain,
                    task_index=task_id,
                    user_simulator=user,
                    agent_model={
                        "provider_id": agent_provider,
                        "model_id": agent_model,
                    },
                    fetch_final_state=not skip_reward,
                    execution_mode=execution_mode,
                )
            except Exception as exc:  # noqa: BLE001
                # Even runner-level failures shouldn't kill the batch —
                # log and move on. Per-task JSON is skipped for this
                # case (no trace produced); the aggregator will note
                # missing files in its summary.
                typer.echo(f"  runner failed for task {task_id}: {exc!r}")
                continue

            typer.echo(
                f"  trace: {len(trace.turns)} turns, stop_reason={trace.stop_reason}, "
                f"duration={trace.total_duration_seconds:.1f}s, error={trace.error}"
            )

            reward: RewardBreakdown | None = None
            if not skip_reward and trace.final_state is not None:
                agent_responses = [
                    t.text for t in trace.turns if t.role == "agent"
                ]
                try:
                    reward = compute_reward(
                        domain=domain,
                        task_index=task_id,
                        final_db_state=trace.final_state,
                        agent_responses=agent_responses,
                    )
                except Exception as exc:  # noqa: BLE001
                    typer.echo(f"  reward computation failed: {exc!r}")

            report_path = write_task_report(
                out_dir=out,
                trace=trace,
                agent_model=f"{agent_provider}:{agent_model}",
                user_model=f"{user_provider or 'auto'}:{user_model}",
                user_strategy=user_strategy,
                reward=reward,
            )
            typer.echo(f"  wrote {report_path}")
            if reward is not None:
                typer.echo(
                    f"  reward={reward.reward} "
                    f"outputs_match={reward.outputs_match} "
                    f"db_hash_match={reward.db_hash_match}"
                )

        if not skip_aggregate:
            md_path = write_batch_report(
                out_dir=out,
                domain=domain,
                agent_model=f"{agent_provider}:{agent_model}",
                user_model=f"{user_provider or 'auto'}:{user_model}",
                task_indices=task_indices,
            )
            typer.echo(f"\naggregate report: {md_path}")
            typer.echo(aggregate_batch_to_markdown(out, task_indices=task_indices))


def main() -> int:
    """Setuptools entry point — calls into typer's app dispatcher."""
    app()
    return 0


if __name__ == "__main__":
    sys.exit(main())
