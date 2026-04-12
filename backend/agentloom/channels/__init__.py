"""Channel adapters — pluggable bridges between ChatFlow and external UIs.

ADR-016: every external channel (Discord, Feishu, Slack, WhatsApp, ...)
implements ``ChannelAdapter`` and hangs off a ``ChannelBinding`` row.
The MVP ships an empty set of real adapters, but the ABC and the
``on_external_turn`` engine hook exist from day one so plumbing can be
wired without schema migrations later.
"""

from agentloom.channels.base import (
    ChannelAdapter,
    ExternalTurn,
    FakeAdapter,
)

__all__ = [
    "ChannelAdapter",
    "ExternalTurn",
    "FakeAdapter",
]
