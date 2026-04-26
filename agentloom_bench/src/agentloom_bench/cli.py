"""``agentloom-bench`` CLI — driver for τ-bench tasks against a
running Agentloom backend.

PR 4: single-task mode. ``--task-ids`` accepts a single index for
this PR; range syntax (e.g. ``0-9``) lands in PR 5 alongside batch
report aggregation.

Example:

    agentloom-bench tau-bench \\
        --domain retail \\
        --task-id 0 \\
        --backend-url http://localhost:8000 \\
        --agent-provider 019d83a5-cd69-7103-aced-e2707cb2008a \\
        --agent-model ark-code-latest \\
        --user-provider volcengine \\
        --user-model ark-code-latest \\
        --user-strategy llm \\
        --max-turns 30 \\
        --out runs/2026-04-26-retail-0
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
from .tau_bench.report import write_task_report
from .tau_bench.reward import RewardBreakdown, compute_reward
from .tau_bench.runner import TauBenchRunner

app = typer.Typer(no_args_is_help=True)


@app.command("tau-bench")
def tau_bench(
    domain: str = typer.Option(..., "--domain", help="``retail`` or ``airline``"),
    task_id: int = typer.Option(..., "--task-id", help="Task index in tau_bench's tasks_test"),
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
        "ark-code-latest",
        "--user-model",
        help="Model id for the user simulator (litellm-style).",
    ),
    user_provider: Optional[str] = typer.Option(
        None,
        "--user-provider",
        help="litellm provider prefix for user simulator; ``None`` lets litellm guess.",
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
        help="Directory to write per-task JSON reports.",
    ),
    skip_reward: bool = typer.Option(
        False,
        "--skip-reward",
        help="Skip reward computation (for plumbing-only smoke runs).",
    ),
) -> None:
    """Run a single τ-bench task against the Agentloom backend."""
    asyncio.run(
        _run_one(
            domain=domain,
            task_id=task_id,
            backend_url=backend_url,
            agent_provider=agent_provider,
            agent_model=agent_model,
            user_model=user_model,
            user_provider=user_provider,
            user_strategy=user_strategy,
            max_turns=max_turns,
            out=out,
            skip_reward=skip_reward,
        )
    )


async def _run_one(
    *,
    domain: str,
    task_id: int,
    backend_url: str,
    agent_provider: str,
    agent_model: str,
    user_model: str,
    user_provider: Optional[str],
    user_strategy: str,
    max_turns: int,
    out: Path,
    skip_reward: bool,
) -> None:
    typer.echo(
        f"agentloom-bench: domain={domain} task_id={task_id} "
        f"agent={agent_provider}:{agent_model} user={user_provider}:{user_model}"
    )
    async with httpx.AsyncClient(base_url=backend_url) as http:
        backend = TauBenchBackendClient(http)

        user = build_user_simulator(
            user_model=user_model,
            user_strategy=user_strategy,
            user_provider=user_provider,
        )

        runner = TauBenchRunner(backend, max_turns=max_turns)
        trace = await runner.run_task(
            domain=domain,
            task_index=task_id,
            user_simulator=user,
            agent_model={"provider_id": agent_provider, "model_id": agent_model},
            fetch_final_state=not skip_reward,
        )

        typer.echo(
            f"  trace: {len(trace.turns)} turns, stop_reason={trace.stop_reason}, "
            f"duration={trace.total_duration_seconds:.1f}s, error={trace.error}"
        )

        reward: RewardBreakdown | None = None
        if not skip_reward and trace.final_state is not None:
            agent_responses = [t.text for t in trace.turns if t.role == "agent"]
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


def main() -> int:
    """Setuptools entry point — calls into typer's app dispatcher."""
    app()
    return 0


if __name__ == "__main__":
    sys.exit(main())
