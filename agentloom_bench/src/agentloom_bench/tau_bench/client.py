"""HTTP client wrapper for the Agentloom backend's tau-bench endpoints
plus the standard ``/turns`` API.

Out-of-process by design (see docs/design-tau-bench-integration.md §4):
the runner pretends to be a normal HTTP client so latency / SSE /
failure modes behave realistically. Same wrapper pattern will scale
to BFCL / SWE-bench in later PRs.

Stateless — every call takes the chatflow_id / session_id explicitly,
so the same client instance can drive multiple concurrent tasks (the
runner currently runs them serially, but the client itself doesn't
care).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class SessionInfo:
    """Returned by :meth:`TauBenchBackendClient.create_session`."""

    session_id: str
    chatflow_id: str
    domain: str
    task_index: int
    instruction: str
    num_tools: int


@dataclass(frozen=True)
class TurnResult:
    """Returned by :meth:`TauBenchBackendClient.submit_turn`. Mirrors
    the backend's ``SubmitTurnResponse`` schema."""

    node_id: str
    status: str
    agent_response: str


class TauBenchBackendClient:
    """Thin async wrapper over the backend HTTP API.

    The caller is responsible for constructing + closing the underlying
    :class:`httpx.AsyncClient`. Decoupling lets tests inject an ASGI-
    transport client pointed at an in-process FastAPI app instead of
    a real network endpoint.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        timeout_seconds: float = 600.0,
    ) -> None:
        self._http = http
        self._timeout = timeout_seconds

    async def create_session(
        self,
        *,
        domain: str,
        task_index: int,
        agent_model: dict[str, str] | None = None,
        title: str | None = None,
    ) -> SessionInfo:
        """``POST /api/tau-bench/sessions``."""
        payload: dict[str, Any] = {"domain": domain, "task_index": task_index}
        if agent_model is not None:
            payload["agent_model"] = agent_model
        if title is not None:
            payload["title"] = title
        resp = await self._http.post(
            "/api/tau-bench/sessions",
            json=payload,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        return SessionInfo(
            session_id=body["session_id"],
            chatflow_id=body["chatflow_id"],
            domain=body["domain"],
            task_index=body["task_index"],
            instruction=body["instruction"],
            num_tools=body["num_tools"],
        )

    async def submit_turn(
        self,
        chatflow_id: str,
        text: str,
        *,
        parent_id: str | None = None,
        spawn_model: dict[str, str] | None = None,
    ) -> TurnResult:
        """``POST /api/chatflows/{id}/turns``. Returns the agent's
        final ``agent_response`` text along with status + node_id.

        ``parent_id=None`` lets the backend append to the latest leaf
        of the chatflow's chain (the typical case for a τ-bench loop:
        each new user message is a child of the previous turn). Pass
        an explicit id to fork.
        """
        payload: dict[str, Any] = {"text": text}
        if parent_id is not None:
            payload["parent_id"] = parent_id
        if spawn_model is not None:
            payload["spawn_model"] = spawn_model
        resp = await self._http.post(
            f"/api/chatflows/{chatflow_id}/turns",
            json=payload,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        return TurnResult(
            node_id=body["node_id"],
            status=body["status"],
            agent_response=body["agent_response"],
        )

    async def get_session_state(self, session_id: str) -> dict[str, Any]:
        """``GET /api/tau-bench/sessions/{id}/state``. Returns the
        session's current mock DB snapshot ({session_id, domain,
        data}). Used after task completion for reward computation.
        """
        resp = await self._http.get(
            f"/api/tau-bench/sessions/{session_id}/state",
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    async def teardown_session(self, session_id: str) -> dict[str, Any]:
        """``POST /api/tau-bench/sessions/{id}/teardown``. Idempotent —
        unknown session id returns ``ok: true, unregistered_tools: 0``.
        """
        resp = await self._http.post(
            f"/api/tau-bench/sessions/{session_id}/teardown",
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()
