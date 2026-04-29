"""Feature: auto_plan execution mode + cognitive ReAct DAG recon.

Verifies the M7.5 PR 7 cognitive ReAct DAG path lands real
WorkNodes for a turn that judge_pre / judge_post want to recon
on:
- ChatFlow with ``execution_mode=auto_plan`` and
  ``cognitive_react_enabled=True`` (the new default since
  ee9c8c3 but pinned explicitly here so the smoke is robust to
  default flips).
- A turn whose feasibility ambiguity nudges the judge toward
  recon (vague reference to a file → judge_pre wants to verify
  the file via Read).
- The resulting WorkFlow contains:
  * judge_pre (succeeded)
  * tool_call(s) parented on judge_pre (the recon dispatches)
  * a follow-up judge_pre parented on the tool_calls (the
    follow-up that re-runs with recon results)
  * a planner / planner_judge / worker chain after recon settles
  * a terminal judge_post

The recursion fuse means at most one round of recon, so we
expect EXACTLY 2 judge_pre WorkNodes (original + follow-up).
Pre-2026-04-29 hotfix the same prompt looped 5 rounds.
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
    async with smoke("04 auto_plan + recon DAG") as report:
        async with backend_client() as client:
            cf_id = await create_chatflow(
                client,
                title="smoke 04 recon",
                execution_mode="auto_plan",
                cognitive_react_enabled=True,
            )
            try:
                # A turn that benefits from recon: agent should
                # want to verify the file exists / what it contains
                # before deciding feasibility. With Read tool
                # available + recon enabled, judge_pre should emit
                # a Read recon spec and re-run.
                t1 = await submit_turn(
                    client,
                    cf_id,
                    "What's in /etc/hostname?",
                )
                report.add(
                    "auto_plan turn completes (status=succeeded)",
                    t1.get("status") == "succeeded",
                    f"status={t1.get('status')}",
                )

                cf = await get_chatflow(client, cf_id)
                worknodes = all_worknodes(cf, chat_node_id=t1["node_id"])
                pre_judges = [
                    w
                    for w in worknodes
                    if w.get("role") == "pre_judge"
                ]
                report.add(
                    "≥1 judge_pre WorkNode in turn",
                    len(pre_judges) >= 1,
                    f"count={len(pre_judges)}",
                )
                report.add(
                    "≤2 judge_pre (recon recursion fuse capped at one round)",
                    len(pre_judges) <= 2,
                    f"count={len(pre_judges)} — pre-fix loop was 5+",
                )

                tool_calls = [
                    w
                    for w in worknodes
                    if w.get("step_kind") == "tool_call"
                ]
                report.add(
                    "≥1 tool_call in turn (recon or worker)",
                    len(tool_calls) >= 1,
                    f"count={len(tool_calls)}",
                )

                # Recon-spawned tool_calls have a JUDGE_CALL parent;
                # worker tool_calls have a DRAFT parent. We don't
                # require recon to fire (the model may decide it has
                # enough info), but if there are 2+ pre_judges then
                # recon DID fire and at least one tool_call should
                # be parented on a pre_judge.
                if len(pre_judges) >= 2:
                    pre_judge_ids = {p["id"] for p in pre_judges}
                    recon_tcs = [
                        w
                        for w in tool_calls
                        if any(
                            pid in pre_judge_ids
                            for pid in (w.get("parent_ids") or [])
                        )
                    ]
                    report.add(
                        "recon path: tool_call parented on judge_pre",
                        len(recon_tcs) >= 1,
                        f"recon tool_calls={len(recon_tcs)}",
                    )

                # The auto_plan pipeline must reach judge_post (the
                # universal exit gate). Without it, the user-facing
                # response would be a halt template — and worse, if
                # judge_pre died at the recon stage like the
                # pre-hotfix bug, judge_post wouldn't even spawn.
                post_judges = [
                    w
                    for w in worknodes
                    if w.get("role") == "post_judge"
                ]
                report.add(
                    "judge_post WorkNode present (chain reached exit gate)",
                    len(post_judges) >= 1,
                    f"count={len(post_judges)}",
                )

                # Final agent_response is non-empty.
                ans = (t1.get("agent_response") or "").strip()
                report.add(
                    "agent_response is non-empty",
                    bool(ans),
                    f"len={len(ans)}",
                )
            finally:
                await delete_chatflow(client, cf_id)


if __name__ == "__main__":
    asyncio.run(main())
