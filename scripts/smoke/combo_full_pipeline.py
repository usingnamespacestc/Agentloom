"""Combo: full chatflow life-cycle exercising multiple features.

One scripted scenario that touches:
- Single turn (#1)
- Multi-turn chain (#2)
- Fork from a non-leaf node (#3)
- Manual compact (#4)
- Drill-down via get_node_context (#5)
- Auto_plan + recon DAG (#6) — turn after compact runs in
  auto_plan to exercise the full cognitive pipeline against a
  compacted ancestor

Tests that the features compose without surprising interactions
(e.g. compact + auto_plan + drill-down all play nicely; recon
DAG fires correctly when the WorkFlow runs against a compacted
context).
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
    async with smoke("combo full pipeline") as report:
        async with backend_client() as client:
            cf_id = await create_chatflow(
                client,
                title="smoke combo",
                # Start in direct mode for the planting phase
                # (cheap), switch to auto_plan for the drill-down
                # turn so the cognitive pipeline runs over a
                # compacted ancestor.
                execution_mode="native_react",
                cognitive_react_enabled=True,
                extra_patch={
                    "compact_trigger_pct": None,  # manual only
                    "chatnode_compact_trigger_pct": None,
                },
            )
            try:
                # Phase 1: build a multi-turn chain with a planted
                # fact in turn 1.
                report.section("phase 1 — plant + chat")
                t1 = await submit_turn(
                    client,
                    cf_id,
                    "My favorite color is octarine. Acknowledge.",
                )
                t2 = await submit_turn(
                    client, cf_id, "Tell me a fun fact about cats."
                )
                t3 = await submit_turn(
                    client, cf_id, "Now one about elephants."
                )
                report.add(
                    "3 turns in linear chain",
                    all(
                        x.get("status") == "succeeded"
                        for x in (t1, t2, t3)
                    ),
                )

                # Phase 2: fork from t2 (mid-chain) — verifies the
                # fork rule even when downstream chain is non-empty.
                report.section("phase 2 — fork from mid-chain")
                fork = await submit_turn(
                    client,
                    cf_id,
                    "On a different track: what's 12*7?",
                    parent_id=t2["node_id"],
                )
                report.add(
                    "fork from non-leaf t2",
                    fork.get("status") == "succeeded",
                )

                # Phase 3: compact the main chain at t3.
                report.section("phase 3 — manual compact main chain")
                rcompact = await client.post(
                    f"/api/chatflows/{cf_id}/nodes/{t3['node_id']}"
                    f"/compact",
                    json={
                        "preserve_recent_turns": 1,
                        "compact_instruction": (
                            "Summarize the conversation. The user "
                            "shared a personal fact about a "
                            "favorite color."
                        ),
                    },
                )
                report.add(
                    "POST /compact returns 200",
                    rcompact.status_code == 200,
                    f"status={rcompact.status_code}",
                )

                cf_state = await get_chatflow(client, cf_id)
                compacts = [
                    n
                    for n in (cf_state.get("nodes") or {}).values()
                    if n.get("compact_snapshot") is not None
                ]
                report.add(
                    "compact ChatNode created",
                    len(compacts) >= 1,
                )

                # Phase 4: switch to auto_plan and ask for the
                # planted fact verbatim. Exercises:
                # (a) auto_plan pipeline over a compacted ancestor
                # (b) recon DAG (judge_pre may want to verify)
                # (c) drill-down (get_node_context) to fetch t1
                # (d) judge_post UX (clean user-facing reply, not
                #     "internal error")
                report.section(
                    "phase 4 — auto_plan + drill-down through compact"
                )
                await client.patch(
                    f"/api/chatflows/{cf_id}",
                    json={"default_execution_mode": "auto_plan"},
                )
                drill = await submit_turn(
                    client,
                    cf_id,
                    "What was the exact word I gave you for my "
                    "favorite color earlier? I need that exact "
                    "spelling.",
                )
                report.add(
                    "drill turn returns 200",
                    drill.get("status") == "succeeded",
                    f"status={drill.get('status')}",
                )
                ans = (drill.get("agent_response") or "").lower()
                report.add(
                    "answer recovers exact word 'octarine'",
                    "octarine" in ans,
                    f"text={ans[:200]!r}",
                )

                # Engine signals to verify on the drill turn:
                cf_final = await get_chatflow(client, cf_id)
                drill_wn = all_worknodes(
                    cf_final, chat_node_id=drill["node_id"]
                )
                judge_pre_count = sum(
                    1 for w in drill_wn if w.get("role") == "pre_judge"
                )
                planner_count = sum(
                    1 for w in drill_wn if w.get("role") == "plan"
                )
                worker_count = sum(
                    1 for w in drill_wn if w.get("role") == "worker"
                )
                judge_post_count = sum(
                    1 for w in drill_wn if w.get("role") == "post_judge"
                )
                tool_calls = [
                    w
                    for w in drill_wn
                    if w.get("step_kind") == "tool_call"
                ]
                tool_names = sorted(
                    {w.get("tool_name") or "" for w in tool_calls}
                )
                report.add(
                    "auto_plan pipeline ran (judge_pre + planner + worker + judge_post)",
                    judge_pre_count >= 1
                    and planner_count >= 1
                    and worker_count >= 1
                    and judge_post_count >= 1,
                    f"pre={judge_pre_count} plan={planner_count} "
                    f"worker={worker_count} post={judge_post_count}",
                )
                report.add(
                    "recon recursion fuse caps judge_pre at ≤2",
                    judge_pre_count <= 2,
                    f"count={judge_pre_count}",
                )
                report.add(
                    "drill-down invoked get_node_context",
                    "get_node_context" in tool_names,
                    f"tools={tool_names}",
                )
                report.add(
                    "user-facing reply has no engine-speak halt template",
                    not any(
                        s in ans
                        for s in (
                            "internal error",
                            "system error",
                            "system failure",
                            "execution flow malfunctioned",
                        )
                    ),
                    f"text={ans[:120]!r}",
                )
            finally:
                await delete_chatflow(client, cf_id)


if __name__ == "__main__":
    asyncio.run(main())
