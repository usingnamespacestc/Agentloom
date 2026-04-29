"""Shared helpers for live-backend smoke scripts.

Each smoke script in this directory drives the running Agentloom
backend over HTTP and prints PASS/FAIL per check. Different from
``tests/backend/`` pytest in three ways:

1. Real HTTP — exercises the actual API surface, not in-process
   TestClient. Catches CORS / middleware / serialization issues
   that pytest mocks past.
2. Real provider — uses whatever ``--agent-model`` / ``--provider``
   the env points to (default volcengine doubao free tier) instead
   of a stub. Catches model-side breakage (forced_tool_name not
   honored, JSON-mode ignored, etc.) the stub can't see.
3. Real DB / Redis / SSE — the full stack. Catches FK violations,
   stale runtime caches, and SSE disconnect bugs.

These scripts are NOT a substitute for pytest; they verify the
stack-as-deployed for a release / pre-merge gate. Run them when
you want a "the system actually works" signal, not for tight
inner-loop dev (use pytest for that).

Backend must already be running at ``http://localhost:8000``. If
you change backend code, ``--reload`` (the default in ``make dev``)
picks it up; otherwise restart manually.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx


BACKEND_URL = os.environ.get("AGENTLOOM_BACKEND", "http://localhost:8000")

# Default model for smoke runs. Echoes the convention from
# agentloom-bench: volcengine doubao 2.0 Pro on the free tier so
# repeated runs don't burn paid quota.
DEFAULT_PROVIDER = os.environ.get("AGENTLOOM_SMOKE_PROVIDER", "volcengine")
DEFAULT_MODEL = os.environ.get(
    "AGENTLOOM_SMOKE_MODEL", "doubao-seed-2-0-pro-260215"
)


# ---------------------------------------------------------------------------
# PASS/FAIL printing
# ---------------------------------------------------------------------------


GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"


@dataclass
class SmokeReport:
    """Per-script outcome accumulator. Print at end."""

    name: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    def add(self, label: str, ok: bool, detail: str = "") -> None:
        self.checks.append((label, ok, detail))
        marker = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
        line = f"  {marker} {label}"
        if detail:
            line += f" {DIM}— {detail}{RESET}"
        print(line)

    def section(self, title: str) -> None:
        print(f"\n{YELLOW}── {title} ──{RESET}")

    @property
    def passed(self) -> bool:
        return all(ok for _, ok, _ in self.checks)

    def print_summary(self) -> None:
        passed = sum(1 for _, ok, _ in self.checks if ok)
        total = len(self.checks)
        status = (
            f"{GREEN}PASS{RESET}" if self.passed else f"{RED}FAIL{RESET}"
        )
        print(
            f"\n{status} — {self.name}: {passed}/{total} checks "
            f"in {self.elapsed_seconds:.1f}s"
        )


@asynccontextmanager
async def smoke(name: str, *, timeout: float = 600.0):
    """Async context manager that wraps a smoke script body, prints
    a header, accumulates a report, and exits non-zero on failure.

    Usage::

        async def main():
            async with smoke("01 single turn") as report:
                ...
                report.add("create chatflow", True)
                ...

        if __name__ == "__main__":
            asyncio.run(main())
    """
    print(f"\n{YELLOW}━━━ {name} ━━━{RESET}")
    print(f"{DIM}backend: {BACKEND_URL}{RESET}")
    report = SmokeReport(name=name)
    start = time.monotonic()
    exc: BaseException | None = None
    try:
        # Quick reachability check before body so the script's first
        # check isn't a cryptic ConnectError.
        async with httpx.AsyncClient(
            base_url=BACKEND_URL, timeout=10.0
        ) as probe:
            r = await probe.get("/docs")
            if r.status_code != 200:
                raise RuntimeError(
                    f"backend at {BACKEND_URL} responded {r.status_code} "
                    f"to /docs — is it running? (try ``make dev`` or "
                    f"``uvicorn agentloom.main:app --reload``)"
                )
        try:
            yield report
        except BaseException as e:  # noqa: BLE001 — capture for finally
            exc = e
            raise
    finally:
        report.elapsed_seconds = time.monotonic() - start
        report.print_summary()
        if exc is not None:
            print(f"  {RED}exception: {type(exc).__name__}: {exc}{RESET}")
        if not report.passed or exc is not None:
            sys.exit(1)


# ---------------------------------------------------------------------------
# Backend client helpers
# ---------------------------------------------------------------------------


def _model_ref() -> dict[str, str]:
    """The default ``ProviderModelRef`` shape for smoke runs."""
    return {"provider_id": DEFAULT_PROVIDER, "model_id": DEFAULT_MODEL}


@asynccontextmanager
async def backend_client(timeout: float = 600.0):
    """Async httpx client preconfigured for the smoke backend.

    Default timeout 600s because auto_plan turns with recon DAG +
    drill-down legitimately take 3-7 minutes on volcengine free tier
    (combo pipeline has been observed at ~7 min to phase 3, then
    another auto_plan turn for phase 4). Per-script overrides allow
    quick scripts to keep tighter bounds.
    """
    async with httpx.AsyncClient(
        base_url=BACKEND_URL, timeout=timeout
    ) as client:
        yield client


async def create_chatflow(
    client: httpx.AsyncClient,
    *,
    title: str | None = None,
    execution_mode: str | None = None,
    cognitive_react_enabled: bool | None = None,
    extra_patch: dict[str, Any] | None = None,
) -> str:
    """POST /api/chatflows + optional follow-up PATCH to set the
    execution mode / recon flag / etc. Returns the chatflow id."""
    body = {"title": title} if title else {}
    r = await client.post("/api/chatflows", json=body)
    r.raise_for_status()
    cf_id = r.json()["id"]

    patch: dict[str, Any] = {}
    if execution_mode is not None:
        patch["default_execution_mode"] = execution_mode
    if cognitive_react_enabled is not None:
        patch["cognitive_react_enabled"] = cognitive_react_enabled
    # Default the smoke chatflow to the smoke model so each LLM
    # call uses a known-cheap path.
    patch.setdefault("draft_model", _model_ref())
    patch.setdefault("default_judge_model", _model_ref())
    patch.setdefault("default_tool_call_model", _model_ref())
    patch.setdefault("brief_model", _model_ref())
    if extra_patch:
        patch.update(extra_patch)
    if patch:
        rp = await client.patch(f"/api/chatflows/{cf_id}", json=patch)
        rp.raise_for_status()
    return cf_id


async def submit_turn(
    client: httpx.AsyncClient,
    chatflow_id: str,
    text: str,
    *,
    parent_id: str | None = None,
) -> dict[str, Any]:
    """POST a user turn and return the response body
    ({node_id, status, agent_response})."""
    body: dict[str, Any] = {"text": text}
    if parent_id is not None:
        body["parent_id"] = parent_id
    r = await client.post(
        f"/api/chatflows/{chatflow_id}/turns", json=body
    )
    r.raise_for_status()
    return r.json()


async def get_chatflow(
    client: httpx.AsyncClient, chatflow_id: str
) -> dict[str, Any]:
    r = await client.get(f"/api/chatflows/{chatflow_id}")
    r.raise_for_status()
    return r.json()


async def delete_chatflow(
    client: httpx.AsyncClient, chatflow_id: str
) -> None:
    """Best-effort cleanup; swallow errors so cleanup doesn't mask
    the script's actual verdict."""
    try:
        await client.delete(f"/api/chatflows/{chatflow_id}")
    except Exception:  # noqa: BLE001
        pass


def find_worknode(
    chatflow: dict[str, Any],
    *,
    chat_node_id: str | None = None,
    role: str | None = None,
    step_kind: str | None = None,
) -> dict[str, Any] | None:
    """Walk a chatflow JSON and return the first WorkNode that
    matches the filters. ``chat_node_id=None`` searches every
    chatnode's workflow."""
    nodes = chatflow.get("nodes") or {}
    candidates: list[dict[str, Any]] = []
    for cn_id, cn in nodes.items():
        if chat_node_id is not None and cn_id != chat_node_id:
            continue
        wf = cn.get("workflow") or {}
        for wn in (wf.get("nodes") or {}).values():
            if role is not None and wn.get("role") != role:
                continue
            if step_kind is not None and wn.get("step_kind") != step_kind:
                continue
            candidates.append(wn)
    return candidates[0] if candidates else None


def all_worknodes(
    chatflow: dict[str, Any], *, chat_node_id: str | None = None
) -> list[dict[str, Any]]:
    """All WorkNodes across every chatnode (or one specific
    chatnode), in stable iteration order."""
    out: list[dict[str, Any]] = []
    for cn_id, cn in (chatflow.get("nodes") or {}).items():
        if chat_node_id is not None and cn_id != chat_node_id:
            continue
        wf = cn.get("workflow") or {}
        for wn in (wf.get("nodes") or {}).values():
            out.append(wn)
    return out
