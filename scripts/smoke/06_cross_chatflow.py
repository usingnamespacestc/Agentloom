"""Feature: cross_chatflow get_node_context (workspace toggle).

Verifies the M7.5 PR 8 cross-chatflow read scope, gated by the
workspace's ``allow_cross_chatflow_lookup`` toggle (74bb772):

1. Two chatflows in the same workspace; chatflow A holds a
   specific fact, chatflow B asks for it.
2. Without the toggle, B's worker can't read A — the gate inside
   ``get_node_context`` raises.
3. Flip ``allow_cross_chatflow_lookup=True`` on workspace
   settings; B's next turn can now resolve a node id from A.

The toggle is workspace-scoped because the trust boundary is the
tenant, not the chatflow.
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
    async with smoke("06 cross_chatflow lookup") as report:
        async with backend_client() as client:
            # Snapshot the original workspace setting so we can
            # restore at end (the toggle defaults False; we flip it
            # to True for the second half of the test).
            ws = await client.get("/api/workspace/settings")
            ws.raise_for_status()
            original_allow = ws.json().get(
                "allow_cross_chatflow_lookup", False
            )

            cf_a: str | None = None
            cf_b: str | None = None
            try:
                # Force toggle off for the first phase.
                await client.patch(
                    "/api/workspace/settings",
                    json={"allow_cross_chatflow_lookup": False},
                )

                cf_a = await create_chatflow(
                    client, title="smoke 06 source"
                )
                cf_b = await create_chatflow(
                    client, title="smoke 06 reader"
                )
                report.add(
                    "two chatflows created in same workspace",
                    bool(cf_a and cf_b),
                )

                # Plant a known fact in chatflow A.
                a_turn = await submit_turn(
                    client,
                    cf_a,
                    "Remember: the secret code is QQQ-123-XYZ. "
                    "Just acknowledge.",
                )
                report.add(
                    "A turn 1 stored",
                    a_turn.get("status") == "succeeded",
                )
                a_node_id = a_turn["node_id"]

                # In B, ask the agent to read A directly. With the
                # toggle off, the cross_chatflow gate refuses.
                report.section("phase 1 — toggle OFF")
                b_blocked = await submit_turn(
                    client,
                    cf_b,
                    f"Use get_node_context with scope='cross_chatflow' "
                    f"to read node id {a_node_id} and report what "
                    f"you find. If you can't, just say so plainly.",
                )
                report.add(
                    "B turn returns 200 even with cross_chatflow blocked",
                    b_blocked.get("status") == "succeeded",
                    f"status={b_blocked.get('status')}",
                )
                blocked_text = (
                    b_blocked.get("agent_response") or ""
                ).lower()
                # The dispatcher returns a ToolError which surfaces
                # as is_error=True content. Either the agent says
                # it can't access (good signal) or the secret code
                # isn't in the answer.
                report.add(
                    "B with toggle OFF doesn't surface the secret",
                    "qqq-123-xyz" not in blocked_text,
                    f"text excerpt={blocked_text[:100]!r}",
                )

                # Now flip the toggle and try again.
                report.section("phase 2 — toggle ON")
                await client.patch(
                    "/api/workspace/settings",
                    json={"allow_cross_chatflow_lookup": True},
                )

                b_allowed = await submit_turn(
                    client,
                    cf_b,
                    f"Try again with get_node_context "
                    f"scope='cross_chatflow' for node {a_node_id} — "
                    f"the workspace just enabled cross-chatflow "
                    f"reads. Report exactly what the source node "
                    f"contains.",
                )
                report.add(
                    "B turn 2 returns 200",
                    b_allowed.get("status") == "succeeded",
                )

                # Verify a get_node_context tool_call ran with
                # scope=cross_chatflow.
                cf_b_state = await get_chatflow(client, cf_b)
                turn2 = (cf_b_state.get("nodes") or {}).get(
                    b_allowed["node_id"]
                )
                cross_calls: list[dict] = []
                if turn2 is not None:
                    for w in all_worknodes(
                        cf_b_state, chat_node_id=b_allowed["node_id"]
                    ):
                        if (
                            w.get("step_kind") == "tool_call"
                            and w.get("tool_name") == "get_node_context"
                        ):
                            args = w.get("tool_args") or {}
                            if args.get("scope") == "cross_chatflow":
                                cross_calls.append(w)
                report.add(
                    "B fired get_node_context(scope=cross_chatflow)",
                    len(cross_calls) >= 1,
                    f"count={len(cross_calls)}",
                )

                # The result either succeeded (tool_result.is_error
                # = False) — meaning the gate let it through — or
                # the model didn't actually invoke it (model-quality
                # observation; we don't fail on that).
                if cross_calls:
                    succeeded_calls = [
                        w
                        for w in cross_calls
                        if not (w.get("tool_result") or {}).get(
                            "is_error", False
                        )
                    ]
                    report.add(
                        "≥1 cross_chatflow lookup succeeded (gate let it pass)",
                        len(succeeded_calls) >= 1,
                        f"succeeded={len(succeeded_calls)}/{len(cross_calls)}",
                    )

                allowed_text = (
                    b_allowed.get("agent_response") or ""
                ).lower()
                report.add(
                    "B with toggle ON answer mentions the secret",
                    "qqq-123-xyz" in allowed_text,
                    f"text excerpt={allowed_text[:120]!r}",
                )
            finally:
                # Restore workspace setting + clean up.
                try:
                    await client.patch(
                        "/api/workspace/settings",
                        json={
                            "allow_cross_chatflow_lookup": original_allow
                        },
                    )
                except Exception:  # noqa: BLE001
                    pass
                if cf_a is not None:
                    await delete_chatflow(client, cf_a)
                if cf_b is not None:
                    await delete_chatflow(client, cf_b)


if __name__ == "__main__":
    asyncio.run(main())
