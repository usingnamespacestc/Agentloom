"""In-memory SSE event bus.

One ``EventBus`` per process; subscribers attach by ``workflow_id`` and
receive ``WorkflowEvent`` objects via an ``asyncio.Queue``. Redis
streams replace this in M11+ for multi-worker deployments.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, Field

from agentloom.schemas.common import utcnow


class WorkflowEvent(BaseModel):
    """Serializable SSE payload."""

    workflow_id: str
    kind: str  # "node.running" | "node.succeeded" | "node.failed" | "workflow.completed"
    node_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    at: str = Field(default_factory=lambda: utcnow().isoformat())


class EventBus:
    """Minimal pub/sub by workflow_id."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[WorkflowEvent | None]]] = defaultdict(list)

    async def publish(self, event: WorkflowEvent) -> None:
        for q in list(self._subscribers.get(event.workflow_id, [])):
            await q.put(event)

    async def close(self, workflow_id: str) -> None:
        """Signal end-of-stream to all current subscribers of a workflow."""
        for q in list(self._subscribers.get(workflow_id, [])):
            await q.put(None)

    def open_subscription(self, workflow_id: str) -> asyncio.Queue[WorkflowEvent | None]:
        """Synchronously register a subscriber and return its queue.

        Use this when the caller needs the subscriber to be visible to
        ``publish``/``close`` *before* yielding control to the event
        loop — e.g. ``ChatFlowEngine`` registers the relay's subscriber
        before awaiting inner-workflow execution, so a fast workflow
        whose ``close`` lands before the relay task has been scheduled
        still delivers the end-of-stream sentinel.
        """
        q: asyncio.Queue[WorkflowEvent | None] = asyncio.Queue()
        self._subscribers[workflow_id].append(q)
        return q

    async def drain(
        self,
        workflow_id: str,
        q: asyncio.Queue[WorkflowEvent | None],
    ) -> AsyncIterator[WorkflowEvent]:
        """Iterate events from a queue obtained via ``open_subscription``."""
        try:
            while True:
                event = await q.get()
                if event is None:
                    return
                yield event
        finally:
            if q in self._subscribers.get(workflow_id, []):
                self._subscribers[workflow_id].remove(q)

    async def subscribe(self, workflow_id: str) -> AsyncIterator[WorkflowEvent]:
        """Convenience wrapper for callers that don't need the
        register-before-yield guarantee (e.g. SSE HTTP routes)."""
        q = self.open_subscription(workflow_id)
        async for event in self.drain(workflow_id, q):
            yield event


# Process-wide singleton used by FastAPI routes.
_default_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _default_bus
    if _default_bus is None:
        _default_bus = EventBus()
    return _default_bus
