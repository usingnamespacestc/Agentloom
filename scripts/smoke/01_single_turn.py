"""Feature: 单线程对话 (single turn).

Verifies the simplest path end-to-end through the live HTTP API:
- POST /api/chatflows creates a chatflow.
- POST /api/chatflows/{id}/turns submits a user message and returns
  the agent's reply synchronously.
- The reply has non-empty text + a node_id.
- A follow-up GET shows exactly 1 ChatNode with status=succeeded
  and the agent_response text matches the synchronous response.

Run from the repo root::

    python scripts/smoke/01_single_turn.py

Exit code 0 on PASS, 1 on FAIL.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Allow running as ``python scripts/smoke/01_*.py`` without -m.
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
    async with smoke("01 single turn") as report:
        async with backend_client() as client:
            cf_id = await create_chatflow(
                client, title="smoke 01 single turn"
            )
            report.add(
                "create chatflow",
                bool(cf_id),
                f"id={cf_id[-12:]}",
            )

            try:
                resp = await submit_turn(
                    client, cf_id, "Say 'hello' in five words or less."
                )
                report.add(
                    "submit_turn returns 200",
                    True,
                    f"status={resp.get('status')}",
                )
                report.add(
                    "agent_response is non-empty",
                    bool((resp.get("agent_response") or "").strip()),
                    f"text={(resp.get('agent_response') or '')[:60]!r}",
                )
                report.add(
                    "node_id is present",
                    bool(resp.get("node_id")),
                    f"node_id={(resp.get('node_id') or '')[-12:]}",
                )

                cf = await get_chatflow(client, cf_id)
                nodes = cf.get("nodes") or {}
                # Chatflow seeds an empty-user welcome node at create
                # time; the user's first turn is the second node.
                # Filter on non-empty user_message to find the real turn.
                user_nodes = [
                    n
                    for n in nodes.values()
                    if (n.get("user_message") or {}).get("text")
                ]
                report.add(
                    "exactly 1 user-submitted ChatNode",
                    len(user_nodes) == 1,
                    f"total nodes={len(nodes)}, user nodes={len(user_nodes)}",
                )
                only = user_nodes[0] if user_nodes else None
                report.add(
                    "user ChatNode status=succeeded",
                    only is not None and only.get("status") == "succeeded",
                    f"got={only.get('status') if only else None}",
                )
                stored = (
                    (only.get("agent_response") or {}).get("text") or ""
                ).strip() if only else ""
                report.add(
                    "stored agent_response matches sync reply",
                    bool(stored)
                    and stored == (resp.get("agent_response") or "").strip(),
                )
            finally:
                await delete_chatflow(client, cf_id)


if __name__ == "__main__":
    asyncio.run(main())
