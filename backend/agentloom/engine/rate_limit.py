"""Rate limiter for provider (LLM) calls.

Round A uses a simple ``asyncio.Semaphore`` to cap concurrent in-flight
LLM requests across the whole process. This is the primitive the
chatflow scheduler relies on to keep parallel branches honest: branches
run in parallel, but every provider call goes through this choke point.

The wrapper respects the :data:`ProviderCall` signature exactly so it
can be dropped in front of any existing provider without code changes
downstream. The semaphore is shared by construction — multiple
wrappers sharing the same semaphore coordinate their concurrency.

Round B will upgrade the internal primitive to a real token bucket
(rpm / tpm / per-model quotas) behind the same ``__call__`` surface, so
nothing upstream of this file needs to move.
"""

from __future__ import annotations

import asyncio

from agentloom.engine.workflow_engine import ProviderCall
from agentloom.providers.types import ChatResponse, Message, ToolDefinition

#: Default max concurrent provider calls. Round A constant; Round B
#: will make this per-provider / per-model via the token bucket.
DEFAULT_MAX_CONCURRENT_LLM_CALLS = 4


class RateLimitedProvider:
    """Wrap a ``ProviderCall`` with an async semaphore.

    Usage::

        sem = asyncio.Semaphore(4)
        wrapped = RateLimitedProvider(raw_provider_call, sem)
        # pass ``wrapped`` wherever a ProviderCall is expected

    Note: because ``ProviderCall`` is a ``Callable`` type alias, this
    class is declared callable via ``__call__`` and will duck-type into
    any place expecting a ProviderCall.
    """

    def __init__(self, inner: ProviderCall, semaphore: asyncio.Semaphore) -> None:
        self._inner = inner
        self._sem = semaphore

    async def __call__(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        model: str | None,
    ) -> ChatResponse:
        async with self._sem:
            return await self._inner(messages, tools, model)


#: Process-wide semaphore shared by all ChatFlowRuntime instances.
#: Lazy-init so tests can override via ``set_global_llm_semaphore``.
_global_sem: asyncio.Semaphore | None = None


def get_global_llm_semaphore() -> asyncio.Semaphore:
    """Return the process-wide LLM semaphore, creating it on first use.

    The first caller fixes the concurrency budget for the process.
    Tests that want a different budget should call
    :func:`set_global_llm_semaphore` before any chatflow runs.
    """
    global _global_sem
    if _global_sem is None:
        _global_sem = asyncio.Semaphore(DEFAULT_MAX_CONCURRENT_LLM_CALLS)
    return _global_sem


def set_global_llm_semaphore(semaphore: asyncio.Semaphore | None) -> None:
    """Replace the global semaphore. Pass ``None`` to force re-creation
    at the default budget on next :func:`get_global_llm_semaphore`."""
    global _global_sem
    _global_sem = semaphore
