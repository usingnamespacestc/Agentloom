"""``ChannelAdapter`` ABC and in-memory ``FakeAdapter``.

The adapter contract is intentionally minimal for MVP: accept an
external turn, deliver an agent response. The engine-side hook
``ChatFlowEngine.on_external_turn`` (see ``chatflow_engine``) bridges
the two: receive → submit_user_turn → push agent_response back.

When real channels land (Discord in v1.1, Feishu in v1.2), they'll
subclass ``ChannelAdapter`` and deal with their own auth/webhook/SDK
details. The core engine stays channel-agnostic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

#: Channel-level policy for queued turns when their upstream node
#: fails. Mirrors ``schemas.chatflow.UpstreamFailurePolicy`` — kept as
#: a plain Literal here so the channels package doesn't import schemas.
UpstreamFailurePolicy = Literal["discard", "continue"]


@dataclass
class ExternalTurn:
    """One inbound message from an external channel.

    Intentionally flat/serializable so the same shape can be sent over
    a message queue in future deployments.
    """

    channel_id: str  # e.g. "discord" | "feishu" | "fake"
    binding_id: str  # maps to ``channel_bindings.id``
    external_user_id: str  # channel-native user identifier
    text: str
    attachments: list[str] = field(default_factory=list)  # blob refs
    metadata: dict[str, Any] = field(default_factory=dict)
    #: What to do with this turn if the node it's queued behind fails
    #: before it runs. Default ``discard`` — Round A treats failures
    #: as conversation breakers and notifies the channel out-of-band.
    on_upstream_failure: UpstreamFailurePolicy = "discard"


# Signature the engine side hands to adapters: given an external turn,
# produce the agent's reply text. The engine owns the ChatFlow; the
# adapter owns channel delivery.
TurnHandler = Callable[[ExternalTurn], Awaitable[str]]


class ChannelAdapter(ABC):
    """Pluggable external-channel bridge.

    Lifetime: one instance per channel_id + binding_id. ``start()`` is
    called once at process boot (to register webhooks, open long-polls,
    etc). ``stop()`` is called at shutdown.
    """

    #: Stable channel identifier; must match ``channel_bindings.kind``.
    channel_id: str = "abstract"

    def __init__(self, binding_id: str, handler: TurnHandler) -> None:
        self.binding_id = binding_id
        self._handler = handler

    @abstractmethod
    async def start(self) -> None:
        """Begin listening for external turns."""

    @abstractmethod
    async def stop(self) -> None:
        """Tear down any resources opened by ``start``."""

    @abstractmethod
    async def send(self, text: str, *, reply_to: str | None = None) -> None:
        """Deliver an agent-side reply out over the channel."""


class FakeAdapter(ChannelAdapter):
    """In-memory test double.

    - ``inject(turn)`` is the entry point for tests to simulate an
      external message arriving. It calls the handler and stores the
      reply in ``self.sent`` — no network, no sleeps, no background
      tasks.
    - ``send`` is a no-op; the fake has no real "outside" to deliver
      to, the reply is captured via ``inject`` directly.
    """

    channel_id = "fake"

    def __init__(self, binding_id: str, handler: TurnHandler) -> None:
        super().__init__(binding_id=binding_id, handler=handler)
        self.sent: list[str] = []
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def send(self, text: str, *, reply_to: str | None = None) -> None:
        self.sent.append(text)

    async def inject(self, turn: ExternalTurn) -> str:
        """Simulate an inbound turn from the fake channel.

        Returns the agent reply so tests can assert on it directly;
        also appends to ``self.sent`` as if the reply had been delivered.
        """
        if not self.started:
            raise RuntimeError("FakeAdapter.inject called before start()")
        reply = await self._handler(turn)
        await self.send(reply, reply_to=turn.external_user_id)
        return reply
