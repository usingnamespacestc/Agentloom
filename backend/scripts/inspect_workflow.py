"""Dump a running ChatFlow / ChatNode / WorkFlow / WorkNode as a chain view.

Usage:
    python backend/scripts/inspect_workflow.py <uuid> [--url http://localhost:8000]

Accepts any of: ChatFlow id, ChatNode id, inner WorkFlow id, inner WorkNode id.
The script hits the public REST API, scans all chatflows for a match, and
prints the enclosing chain (who-contains-who) plus a topologically-ordered
dump of the target WorkFlow — one line per WorkNode, with step_kind, status,
parents, model, token usage, tool_name/args (truncated), and tool_result.

Designed for eyeballing how the ReAct loop unfolds during development.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from typing import Any


def fetch(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def trunc(s: str, n: int = 120) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _short(uid: str | None) -> str:
    return (uid or "")[:8]


def dump_worknode(wn: dict, is_target: bool) -> None:
    marker = ">>" if is_target else "  "
    parents = [_short(p) for p in wn.get("parent_ids", [])]
    step = wn.get("step_kind")
    status = wn.get("status")
    print(f"  {marker} {_short(wn['id'])}  {step:24s} {status:10s} parents={parents}")

    mo = wn.get("model_override")
    if mo:
        pid = _short(mo.get("provider_id"))
        print(f"          model: {pid}:{mo.get('model_id')}")

    tc = wn.get("tool_constraints")
    if tc and (tc.get("allow") or tc.get("deny")):
        print(f"          tool_constraints: allow={tc.get('allow')} deny={tc.get('deny')}")

    if step == "draft":
        u = wn.get("usage")
        if u:
            print(
                f"          usage: prompt={u['prompt_tokens']} "
                f"completion={u['completion_tokens']} cached={u['cached_tokens']}"
            )
        inputs = wn.get("input_messages") or []
        if inputs:
            for i, msg in enumerate(inputs):
                print(f"          in[{i}] {msg['role']:9s}: {trunc(msg.get('content') or '')}")
        om = wn.get("output_message")
        if om:
            if om.get("content"):
                print(f"          out    text: {trunc(om['content'])}")
            for tu in om.get("tool_uses") or []:
                args = json.dumps(tu.get("arguments", {}), ensure_ascii=False)
                print(f"          out    tool_use: {tu['name']}({trunc(args, 100)})")

    elif step == "tool_call":
        print(f"          tool: {wn.get('tool_name')}")
        if wn.get("tool_args"):
            print(f"          args: {trunc(json.dumps(wn['tool_args'], ensure_ascii=False), 160)}")
        tr = wn.get("tool_result")
        if tr:
            err_tag = " [ERROR]" if tr.get("is_error") else ""
            print(f"          result{err_tag}: {trunc(tr.get('content') or '', 200)}")

    if wn.get("error"):
        print(f"          ERROR: {wn['error']}")


def dump_workflow(wf: dict, target_id: str | None) -> None:
    print(f"--- WorkFlow {_short(wf['id'])} ({len(wf['nodes'])} nodes, roots={[_short(r) for r in wf.get('root_ids', [])]})")
    # crude topological order: walk from roots via parent_ids
    nodes: dict[str, dict] = wf["nodes"]
    order: list[str] = []
    seen: set[str] = set()

    def walk(nid: str) -> None:
        if nid in seen:
            return
        for p in nodes[nid].get("parent_ids", []):
            if p in nodes:
                walk(p)
        seen.add(nid)
        order.append(nid)

    for nid in nodes:
        walk(nid)
    for nid in order:
        dump_worknode(nodes[nid], is_target=(nid == target_id))


def locate(uid: str, base: str) -> tuple[dict, str, str | None]:
    """Return (chatflow, kind, target_id_inside) where kind is one of:
    ``chatflow``, ``chatnode``, ``workflow``, ``worknode``.
    ``target_id_inside`` is the worknode/chatnode to highlight (or None)."""
    cfs = fetch(f"{base}/api/chatflows")
    for cf_meta in cfs:
        if cf_meta["id"] == uid:
            return fetch(f"{base}/api/chatflows/{uid}"), "chatflow", None
    for cf_meta in cfs:
        cf = fetch(f"{base}/api/chatflows/{cf_meta['id']}")
        if uid in cf["nodes"]:
            return cf, "chatnode", uid
        for cn in cf["nodes"].values():
            wf = cn.get("workflow") or {}
            if wf.get("id") == uid:
                return cf, "workflow", cn["id"]
            if uid in (wf.get("nodes") or {}):
                return cf, "worknode", uid
    raise SystemExit(f"id {uid} not found in any chatflow")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("uid", help="ChatFlow / ChatNode / WorkFlow / WorkNode id")
    ap.add_argument("--url", default="http://localhost:8000", help="backend base URL")
    args = ap.parse_args()

    cf, kind, target = locate(args.uid, args.url.rstrip("/"))
    print(f"Resolved {args.uid[:8]}… as {kind} in ChatFlow {cf['id'][:8]}…")
    print(f"  title: {cf.get('title') or '(untitled)'}")
    print(f"  draft_model: {cf.get('draft_model')}")
    print()

    if kind == "chatflow":
        print(f"ChatFlow has {len(cf['nodes'])} ChatNodes, roots={[_short(r) for r in cf['root_ids']]}")
        for cnid, cn in cf["nodes"].items():
            um = (cn.get("user_message") or {}).get("text") or ""
            ar = (cn.get("agent_response") or {}).get("text") or ""
            print(f"  {_short(cnid)} status={cn['status']:10s} user={trunc(um, 50)!r}")
            print(f"           agent={trunc(ar, 80)!r}")
        return

    if kind == "chatnode":
        cn = cf["nodes"][target]  # type: ignore[index]
    else:
        cn = next(
            c for c in cf["nodes"].values()
            if (c.get("workflow") or {}).get("id") == target
            or target in (c.get("workflow") or {}).get("nodes", {})
        )

    print(f"ChatNode {_short(cn['id'])} status={cn['status']}")
    um = (cn.get("user_message") or {}).get("text") or ""
    ar = (cn.get("agent_response") or {}).get("text") or ""
    print(f"  user_message:    {trunc(um, 200)}")
    print(f"  agent_response:  {trunc(ar, 200)}")
    print()

    wf = cn.get("workflow") or {}
    wn_target = target if kind == "worknode" else None
    dump_workflow(wf, wn_target)


if __name__ == "__main__":
    sys.exit(main())
