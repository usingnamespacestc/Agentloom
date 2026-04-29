"""Feature: 多线程分支 (fork).

Verifies:
- Submitting a turn with ``parent_id`` pointing at an existing
  ChatNode forks a new branch from that point.
- The fork doesn't mutate the existing branch's children.
- Both branches share the parent ancestor and diverge after.

The fork-semantics memory says forks must NEVER reject (a turn
into a non-leaf must always succeed by forking, never by
appending).
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


def _user_chain(chatflow: dict) -> list[str]:
    """Ids of user-submitted ChatNodes in chronological order."""
    nodes = chatflow.get("nodes") or {}
    user_only = [
        n
        for n in nodes.values()
        if (n.get("user_message") or {}).get("text")
    ]
    user_only.sort(key=lambda n: n.get("created_at", ""))
    return [n["id"] for n in user_only]


async def main() -> None:
    async with smoke("02 fork branch") as report:
        async with backend_client() as client:
            cf_id = await create_chatflow(client, title="smoke 02 fork")
            try:
                # Build a 2-turn linear chain.
                t1 = await submit_turn(client, cf_id, "What's 2+2?")
                t2 = await submit_turn(
                    client, cf_id, "Now what's 5+5?"
                )
                report.add(
                    "linear chain has 2 user turns",
                    bool(t1.get("node_id") and t2.get("node_id")),
                )

                cf = await get_chatflow(client, cf_id)
                chain = _user_chain(cf)
                report.add(
                    "chain order matches submission order",
                    len(chain) == 2 and chain[1] == t2["node_id"],
                )

                # Fork from t1 — submit a turn with parent_id=t1.
                # Even though t1 isn't the leaf (t2 is), the fork
                # rule says this must succeed by forking, never by
                # appending to t2.
                fork = await submit_turn(
                    client,
                    cf_id,
                    "Different question: what's the capital of France?",
                    parent_id=t1["node_id"],
                )
                report.add(
                    "fork submit_turn returns 200",
                    fork.get("status") == "succeeded",
                    f"status={fork.get('status')}",
                )

                cf2 = await get_chatflow(client, cf_id)
                # The fork node must have parent_ids = [t1.node_id]
                fork_node = (cf2.get("nodes") or {}).get(
                    fork["node_id"]
                )
                report.add(
                    "fork node exists in chatflow",
                    fork_node is not None,
                )
                if fork_node is not None:
                    parent_ids = fork_node.get("parent_ids") or []
                    report.add(
                        "fork parent_ids is [t1]",
                        parent_ids == [t1["node_id"]],
                        f"got={parent_ids}",
                    )

                # t2 must still be there with the same parent (t1).
                t2_node = (cf2.get("nodes") or {}).get(t2["node_id"])
                report.add(
                    "original t2 still present (fork didn't replace)",
                    t2_node is not None,
                )
                if t2_node is not None:
                    report.add(
                        "t2's parent_ids unchanged",
                        t2_node.get("parent_ids") == [t1["node_id"]],
                    )

                # The fork response talks about France, not 5+5 — the
                # provider only sees the forked branch's history.
                fork_text = (fork.get("agent_response") or "").lower()
                report.add(
                    "fork agent_response addresses fork question",
                    "paris" in fork_text or "france" in fork_text,
                    f"text={fork_text[:80]!r}",
                )
            finally:
                await delete_chatflow(client, cf_id)


if __name__ == "__main__":
    asyncio.run(main())
