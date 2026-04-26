"""τ-bench tool wrapper + per-session source.

A "session" here is one task instance: a fresh mock DB dict (e.g. a
copy of the retail orders/users/products from
``tau_bench.envs.retail.data.load_data()``), plus a set of tool
wrapper instances that mutate that dict when invoked. Sessions are
keyed by session id (1:1 with chatflow id) so two concurrent retail
tasks don't share state.

Tool name format: ``<wrapper_prefix><snake_name>`` where wrapper_prefix
defaults to ``tau_<short_session>_``. Per-session prefixing is what
makes concurrent task runs safe even though the backend's
``ToolRegistry`` is global — two sessions registering the same upstream
tool class get different registered names.

Backend does NOT import upstream agent loops or user simulators —
the runner drives those out-of-process. We only need the tool *classes*
here (their ``invoke()`` static methods are the ground truth that
``calculate_reward()`` later replays for hash comparison).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from agentloom.schemas.common import ToolResult
from agentloom.tools.base import Tool, ToolContext, ToolError, ToolRegistry

log = logging.getLogger(__name__)


# CamelCase-to-snake_case converter — tau_bench tool classes are
# ``GetOrderDetails``, ``CancelPendingOrder`` etc.; their advertised
# tool name (in get_info()["function"]["name"]) is the snake form.
_CAMEL_TO_SNAKE = re.compile(r"(?<!^)(?=[A-Z])")


def _snake(camel: str) -> str:
    return _CAMEL_TO_SNAKE.sub("_", camel).lower()


class TauBenchToolWrapper(Tool):
    """Adapt a tau_bench tool class (with a ``static invoke(data, **args)``
    method and an ``static get_info() -> {function: {name, description,
    parameters}}``) to Agentloom's :class:`Tool` ABC.

    The wrapper holds a reference to the **session's mock DB dict** so
    each invocation mutates the same dict. When the wrapper is
    unregistered (via :class:`TauBenchToolSource.close`), the dict is
    released; subsequent calls would no-op since the wrapper is no
    longer reachable from the registry.

    Two intentional design choices:

    1. ``side_effect = "write"`` (will land alongside M7.5 capability
       model — for now the field doesn't exist on Tool yet, but the
       semantic is that retail tools mutate state). The base ``Tool``
       ABC has no side_effect field today, so this is documentation
       only; M7.5 will surface it.

    2. We run upstream's synchronous ``invoke`` in ``asyncio.to_thread``
       to avoid blocking the engine's event loop. Most retail/airline
       tools are pure dict operations and finish in <1ms, but a few
       (``Calculate``) do compute that's not always trivial.
    """

    def __init__(
        self,
        tau_tool_cls: type,
        env_data: dict[str, Any],
        *,
        registered_name: str,
    ) -> None:
        info = tau_tool_cls.get_info()
        fn = info.get("function", {})
        self._tau_tool_cls = tau_tool_cls
        self._env_data = env_data
        # Override Tool's class-level attrs on the instance so the
        # session prefix and per-session description show up to the LLM.
        self.name = registered_name
        self.description = fn.get("description", "")
        self.parameters = fn.get("parameters", {"type": "object", "properties": {}})

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            raw = await asyncio.to_thread(
                self._tau_tool_cls.invoke, self._env_data, **args
            )
        except TypeError as exc:
            # Bad args (missing required, wrong type) — surface as an
            # explicit ToolError so the engine emits is_error=True.
            raise ToolError(f"{self.name}: bad arguments — {exc}") from exc
        # tau_bench tools return either a JSON-serializable string or a
        # plain string ("Error: ...") when nothing matched. We pass it
        # through as the content so the LLM sees the ground-truth
        # response format that calculate_reward also sees.
        if isinstance(raw, str):
            return ToolResult(content=raw)
        return ToolResult(content=json.dumps(raw, ensure_ascii=False))


class TauBenchToolSource:
    """Bundle of wrapper tools for one session.

    Lifecycle:

    1. ``__init__`` builds wrapper instances bound to ``env_data``;
       does NOT register them yet.
    2. ``connect_and_register(registry)`` registers all wrappers under
       prefixed names. Returns the list of registered names.
    3. ``close(registry)`` unregisters every name we hold.

    Concurrency: sessions are independent — each holds its own
    ``env_data`` dict, so two sessions can run in parallel as long as
    their registered names don't collide (guaranteed by the prefix).
    """

    def __init__(
        self,
        *,
        session_id: str,
        domain: str,
        env_data: dict[str, Any],
        tool_classes: list[type],
        prefix: str | None = None,
    ) -> None:
        if domain not in {"retail", "airline"}:
            raise ValueError(f"unknown tau-bench domain {domain!r}")
        self.session_id = session_id
        self.domain = domain
        self.env_data = env_data
        self.registered_names: list[str] = []
        # Default prefix: short suffix of session id keeps tool names
        # readable but globally unique. ``tau_<6-char>_<snake>``.
        self._prefix = prefix or f"tau_{session_id[-6:]}_"
        # Build wrappers eagerly; they're cheap and we want to fail
        # early (e.g. if a tool class doesn't conform to the
        # ``invoke`` / ``get_info`` contract).
        self._wrappers: list[TauBenchToolWrapper] = []
        for cls in tool_classes:
            registered_name = f"{self._prefix}{_snake(cls.__name__)}"
            self._wrappers.append(
                TauBenchToolWrapper(
                    cls, env_data=env_data, registered_name=registered_name
                )
            )

    def tool_names(self) -> list[str]:
        """Names the wrappers WILL register under; available before
        ``connect_and_register`` for ChatFlow.disabled_tool_names
        computation."""
        return [w.name for w in self._wrappers]

    def connect_and_register(self, registry: ToolRegistry) -> list[str]:
        if self.registered_names:
            raise RuntimeError(
                f"tau-bench session {self.session_id!r} already registered"
            )
        for w in self._wrappers:
            registry.register(w)
            self.registered_names.append(w.name)
        log.info(
            "tau-bench: registered %d tools for session %s (domain=%s)",
            len(self.registered_names),
            self.session_id,
            self.domain,
        )
        return list(self.registered_names)

    def close(self, registry: ToolRegistry) -> None:
        """Unregister all tools owned by this session. Idempotent."""
        for name in list(self.registered_names):
            registry.unregister(name)
        if self.registered_names:
            log.info(
                "tau-bench: unregistered %d tools for session %s",
                len(self.registered_names),
                self.session_id,
            )
        self.registered_names = []
