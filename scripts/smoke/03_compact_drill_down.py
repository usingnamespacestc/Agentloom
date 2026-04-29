"""Feature: compact (manual) + drill-down via get_node_context.

Two related features in one script because they're naturally
exercised together:

1. **Manual compact** (Tier 2): a long chain of turns can be
   summarized into a compact ChatNode that becomes the new
   ancestor cutoff for downstream context builds.
2. **Drill-down**: after compact, a follow-up turn that asks for
   a verbatim detail from the compacted history triggers the
   ``get_node_context`` tool — the agent fetches the original
   ChatNode and answers from it instead of guessing from the
   summary.

The drill-down hint that ships with system_envelope is supposed
to nudge models toward this when ``packed_range`` /
``compact_snapshot`` ancestors exist; this smoke checks the
trigger fires end-to-end with a real model.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _common import (  # noqa: E402
    backend_client,
    create_chatflow,
    delete_chatflow,
    get_chatflow,
    smoke,
    submit_turn,
)


async def main() -> None:
    async with smoke("03 compact + drill-down") as report:
        async with backend_client() as client:
            cf_id = await create_chatflow(
                client,
                title="smoke 03 compact",
                # Tier 1 (auto pre-llm_call) off so the compact we
                # see is the manual one, not an auto trigger.
                extra_patch={
                    "compact_trigger_pct": None,
                    "chatnode_compact_trigger_pct": None,
                },
            )
            try:
                # Plant a specific fact in turn 1, then chat about
                # something else for several turns so the original
                # fact is "buried" in history.
                report.section("planting fact + chat")
                await submit_turn(
                    client,
                    cf_id,
                    "Remember this: my lucky number is 8473921. "
                    "It's important you remember it exactly.",
                )
                await submit_turn(
                    client, cf_id, "What's the capital of Italy?"
                )
                await submit_turn(
                    client, cf_id, "And what about Japan?"
                )
                t4 = await submit_turn(
                    client, cf_id, "Tell me a one-line joke."
                )
                report.add("planted 4 turns", True)

                # Trigger explicit compact at the latest leaf.
                report.section("manual compact")
                rcompact = await client.post(
                    f"/api/chatflows/{cf_id}/nodes/{t4['node_id']}"
                    f"/compact",
                    json={
                        "preserve_recent_turns": 1,
                        "compact_instruction": (
                            "Summarize the conversation. The lucky "
                            "number was a personal fact the user "
                            "asked me to remember."
                        ),
                    },
                )
                report.add(
                    "POST /compact returns 200",
                    rcompact.status_code == 200,
                    f"status={rcompact.status_code}",
                )

                cf_after = await get_chatflow(client, cf_id)
                compact_nodes = [
                    n
                    for n in (cf_after.get("nodes") or {}).values()
                    if n.get("compact_snapshot") is not None
                ]
                report.add(
                    "compact ChatNode created",
                    len(compact_nodes) >= 1,
                    f"count={len(compact_nodes)}",
                )

                # Now ask for a verbatim detail — the lucky number.
                # If drill-down works, agent calls get_node_context
                # to retrieve the original turn 1 and answers
                # exactly. If it answers from the summary alone, the
                # number may be approximated / hallucinated.
                report.section("drill-down ask")
                drill = await submit_turn(
                    client,
                    cf_id,
                    "What was the exact lucky number I told you "
                    "earlier? I need every digit correct.",
                )
                report.add(
                    "drill-down turn returns 200",
                    drill.get("status") == "succeeded",
                    f"status={drill.get('status')}",
                )
                ans = (drill.get("agent_response") or "").strip()
                report.add(
                    "answer mentions exact digits 8473921",
                    "8473921" in ans,
                    f"text={ans[:200]!r}",
                )

                # Verify a get_node_context tool_call actually fired
                # in the drill turn's WorkFlow.
                cf_final = await get_chatflow(client, cf_id)
                drill_node = (cf_final.get("nodes") or {}).get(
                    drill["node_id"]
                )
                tool_calls: list[str] = []
                if drill_node is not None:
                    wf = drill_node.get("workflow") or {}
                    for wn in (wf.get("nodes") or {}).values():
                        if wn.get("step_kind") == "tool_call":
                            tool_calls.append(wn.get("tool_name") or "")
                report.add(
                    "drill turn invoked get_node_context",
                    "get_node_context" in tool_calls,
                    f"tool_calls={tool_calls}",
                )
            finally:
                await delete_chatflow(client, cf_id)


if __name__ == "__main__":
    asyncio.run(main())
