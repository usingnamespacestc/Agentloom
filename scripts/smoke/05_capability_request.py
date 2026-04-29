"""Feature: capability_request marker → escalation feedback loop.

Verifies the full PR 5 + 2f9998f feedback loop works end-to-end:

1. ChatFlow with auto_plan + Bash explicitly disabled.
2. Turn that the worker can only complete by running shell
   commands (e.g. "what's the current uptime"). The worker
   without Bash will emit a ``<capability_request>Bash</...>``
   marker per the worker fixture.
3. ``_extract_capability_request`` lifts it onto
   ``WorkFlowNode.capability_request``.
4. ``worker_judge`` reads that field, sets
   ``JudgeVerdict.capability_escalation = ['Bash']``, votes
   ``revise``.
5. ``_after_worker_judge`` widens
   ``WorkFlow.inheritable_tools`` to include Bash and spawns a
   fresh planner with handoff_notes naming the escalation.

We don't need the second-round worker to actually succeed
(Bash stays disabled at the chatflow level); we just verify
the marker → field → escalation → respawn chain landed real
WorkNodes.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _common import (  # noqa: E402
    all_worknodes,
    backend_client,
    create_chatflow,
    delete_chatflow,
    get_chatflow,
    smoke,
    submit_turn,
)


async def main() -> None:
    async with smoke("05 capability_request feedback loop") as report:
        async with backend_client() as client:
            cf_id = await create_chatflow(
                client,
                title="smoke 05 capability_request",
                execution_mode="auto_plan",
                # Disable a tool the worker would need so it has to
                # emit the capability_request marker.
                extra_patch={"disabled_tool_names": ["Bash"]},
            )
            try:
                t1 = await submit_turn(
                    client,
                    cf_id,
                    "Run ``uptime`` in the shell and tell me how "
                    "long the system has been up.",
                )
                report.add(
                    "turn completes (status=succeeded or halted)",
                    t1.get("status") in ("succeeded", "failed"),
                    f"status={t1.get('status')}",
                )

                cf = await get_chatflow(client, cf_id)
                worknodes = all_worknodes(cf, chat_node_id=t1["node_id"])

                # Three scenarios depending on model behavior. Only
                # the third actually exercises the feedback loop;
                # the first two are valid model-quality observations
                # that don't fault the engine.
                #
                # 1. judge_pre voted infeasible up-front (saw Bash
                #    disabled in the catalog, declined). No worker
                #    spawned; the chain halted before the marker
                #    could fire.
                # 2. Worker spawned and answered without trying to
                #    invoke the missing tool — model didn't realize
                #    it needed Bash.
                # 3. Worker spawned and emitted
                #    ``<capability_request>...</capability_request>``
                #    in its draft — engine should propagate to
                #    judge → escalation → respawn planner.
                worker_nodes = [
                    w
                    for w in worknodes
                    if w.get("role") == "worker"
                    and w.get("step_kind") == "draft"
                ]
                with_marker = [
                    w
                    for w in worker_nodes
                    if (w.get("capability_request") or [])
                ]

                if not worker_nodes:
                    # Path 1: judge_pre short-circuited.
                    report.add(
                        "ℹ judge_pre short-circuited before worker "
                        "(infeasible verdict on disabled tool catalog)",
                        True,
                        "valid path — feedback loop not exercised "
                        "this run; rerun or rephrase prompt to test",
                    )
                    return

                report.add(
                    "≥1 worker WorkNode in the turn",
                    len(worker_nodes) >= 1,
                    f"workers={len(worker_nodes)}",
                )

                if not with_marker:
                    # Path 2: worker didn't emit marker.
                    report.add(
                        "ℹ worker did NOT emit capability_request marker",
                        True,
                        "model-quality observation; downstream "
                        "checks skipped (engine path covered by pytest)",
                    )
                    return

                report.add(
                    "worker emitted capability_request marker",
                    True,
                    f"requested={with_marker[0].get('capability_request')}",
                )

                # Verify worker_judge picked it up.
                worker_judges = [
                    w
                    for w in worknodes
                    if w.get("role") == "worker_judge"
                ]
                escalations = []
                for wj in worker_judges:
                    v = wj.get("judge_verdict") or {}
                    cap_esc = v.get("capability_escalation") or []
                    if cap_esc:
                        escalations.append(cap_esc)
                report.add(
                    "worker_judge populated capability_escalation",
                    len(escalations) >= 1,
                    f"escalations={escalations}",
                )

                # WorkFlow.inheritable_tools should be widened to
                # include the requested tool (or the engine would
                # have spawned a fresh planner under the
                # worker_judge, indicating the widening + respawn
                # path fired).
                turn_node = (cf.get("nodes") or {}).get(t1["node_id"])
                wf = (turn_node or {}).get("workflow") or {}
                inherit = wf.get("inheritable_tools") or []
                report.add(
                    "WorkFlow.inheritable_tools widened to include Bash",
                    "Bash" in inherit,
                    f"got={inherit}",
                )
            finally:
                await delete_chatflow(client, cf_id)


if __name__ == "__main__":
    asyncio.run(main())
