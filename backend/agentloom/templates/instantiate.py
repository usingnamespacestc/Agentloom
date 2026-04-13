"""Turn a persisted ``WorkflowTemplateRow`` + params into a fresh
:class:`agentloom.schemas.WorkFlow` ready to execute or preview.

Resolution
----------
:func:`resolve_template` looks up ``(workspace_id, builtin_id)`` — the
user's workspace first, falling back to the ``__builtin__`` fixture. This
is how user overrides shadow shipped fixtures without the engine having
to know which is which.

Substitution
------------
Two Jinja-style tags are supported. We do **not** use Jinja2 itself —
the subset we need is small, and the dependency isn't worth the risk
of template authors discovering arbitrary expressions.

- ``{{ name }}`` — required. Must appear in the provided params dict;
  missing values raise :class:`UnboundParamError`.
- ``{% include 'frag' %}`` — splices in the ``text`` body of a fragment
  from the ``_*.yaml`` files loaded by :func:`load_fixtures`. Cycles are
  detected when the fragments are loaded, not here.

Substitution runs on every string value in the serialized plan tree:
trio text, WireMessage content, descriptions — basically anywhere the
template author can put a placeholder.

Node id rewriting
-----------------
Fixture node ids are stable, human-readable strings like ``"plan_root"``
so the fixture can reference them. At instantiate time every id is
remapped to a fresh UUIDv7 — including ``parent_ids`` and
``root_ids`` — so multiple instantiations of the same template don't
collide inside one WorkFlow.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentloom.db.models.workflow_template import WorkflowTemplateRow
from agentloom.schemas import WorkFlow
from agentloom.schemas.common import generate_node_id

BUILTIN_WORKSPACE_ID = "__builtin__"

_PARAM_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_INCLUDE_RE = re.compile(r"\{%\s*include\s+'([^']+)'\s*%\}")


class TemplateError(Exception):
    """Base class for template resolution / instantiation failures."""


class TemplateNotFoundError(TemplateError):
    """No row with the given ``builtin_id`` under either the caller's
    workspace or the ``__builtin__`` fallback."""


class UnboundParamError(TemplateError):
    """A ``{{ param }}`` placeholder had no value in the caller's
    ``params`` dict. Message includes the placeholder name(s)."""


class TemplateCycleError(TemplateError):
    """Include fragments form a cycle (A includes B includes A)."""


async def resolve_template(
    session: AsyncSession,
    *,
    workspace_id: str,
    builtin_id: str,
) -> WorkflowTemplateRow:
    """Load the row for *builtin_id*, preferring the user's workspace
    over the shipped fixture.

    Raises :class:`TemplateNotFoundError` if neither row exists.
    """
    stmt = select(WorkflowTemplateRow).where(
        WorkflowTemplateRow.builtin_id == builtin_id,
        WorkflowTemplateRow.workspace_id.in_([workspace_id, BUILTIN_WORKSPACE_ID]),
    )
    rows = (await session.execute(stmt)).scalars().all()
    if not rows:
        raise TemplateNotFoundError(
            f"no template with builtin_id={builtin_id!r} in workspace "
            f"{workspace_id!r} or {BUILTIN_WORKSPACE_ID!r}"
        )
    # Prefer the user workspace row when both exist.
    for row in rows:
        if row.workspace_id == workspace_id:
            return row
    return rows[0]


def _substitute_string(
    text: str,
    params: dict[str, Any],
    includes: dict[str, str],
    *,
    include_stack: tuple[str, ...] = (),
) -> str:
    """Expand ``{% include %}`` (recursively) then ``{{ param }}`` in *text*.

    Include expansion happens before parameter substitution so a fragment
    can itself reference ``{{ param }}``.
    """

    def expand_include(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in include_stack:
            chain = " -> ".join([*include_stack, name])
            raise TemplateCycleError(f"include cycle: {chain}")
        if name not in includes:
            raise TemplateError(f"unknown include fragment {name!r}")
        return _substitute_string(
            includes[name],
            params,
            includes,
            include_stack=(*include_stack, name),
        )

    # Repeatedly apply until no include tags remain; each expansion may
    # introduce new tags we need to walk into.
    prev = None
    while prev != text:
        prev = text
        text = _INCLUDE_RE.sub(expand_include, text)

    missing: list[str] = []

    def expand_param(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in params:
            missing.append(name)
            return match.group(0)
        return str(params[name])

    result = _PARAM_RE.sub(expand_param, text)
    if missing:
        raise UnboundParamError(
            f"unbound param(s): {', '.join(sorted(set(missing)))}"
        )
    return result


def _walk_substitute(
    value: Any,
    params: dict[str, Any],
    includes: dict[str, str],
) -> Any:
    """Recursively substitute every string leaf inside *value*."""
    if isinstance(value, str):
        return _substitute_string(value, params, includes)
    if isinstance(value, list):
        return [_walk_substitute(v, params, includes) for v in value]
    if isinstance(value, dict):
        return {k: _walk_substitute(v, params, includes) for k, v in value.items()}
    return value


def _collect_node_ids(plan: dict[str, Any]) -> set[str]:
    nodes = plan.get("nodes") or {}
    if isinstance(nodes, dict):
        return set(nodes.keys())
    if isinstance(nodes, list):
        return {n["id"] for n in nodes if isinstance(n, dict) and "id" in n}
    return set()


def _remap_ids(plan: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of *plan* with every fixture node id replaced
    by a fresh UUIDv7. ``parent_ids`` and ``root_ids`` are rewritten
    consistently."""
    id_map = {old: generate_node_id() for old in _collect_node_ids(plan)}

    def remap_node(n: dict[str, Any]) -> dict[str, Any]:
        out = dict(n)
        if "id" in out:
            out["id"] = id_map.get(out["id"], out["id"])
        if isinstance(out.get("parent_ids"), list):
            out["parent_ids"] = [id_map.get(p, p) for p in out["parent_ids"]]
        # judge_call's judge_target_id points at another WorkNode id.
        if out.get("judge_target_id") in id_map:
            out["judge_target_id"] = id_map[out["judge_target_id"]]
        return out

    out = dict(plan)
    raw_nodes = out.get("nodes")
    if isinstance(raw_nodes, list):
        remapped = [remap_node(n) for n in raw_nodes]
        # Pydantic's WorkFlow expects nodes as a dict keyed by id.
        out["nodes"] = {n["id"]: n for n in remapped}
    elif isinstance(raw_nodes, dict):
        remapped_dict: dict[str, Any] = {}
        for old_id, n in raw_nodes.items():
            new_node = remap_node({**n, "id": n.get("id", old_id)})
            remapped_dict[new_node["id"]] = new_node
        out["nodes"] = remapped_dict
    if isinstance(out.get("root_ids"), list):
        out["root_ids"] = [id_map.get(r, r) for r in out["root_ids"]]
    # Fresh WorkFlow id on every instantiation.
    out["id"] = generate_node_id()
    return out


def instantiate_template(
    row: WorkflowTemplateRow,
    params: dict[str, Any],
    *,
    includes: dict[str, str] | None = None,
) -> WorkFlow:
    """Produce a fresh :class:`WorkFlow` from *row* with *params* bound.

    *includes* maps fragment names (e.g. ``"_critique_base"``) to their
    raw text. Typically the caller gets this from
    :func:`agentloom.templates.loader.load_fixtures`. ``None`` means no
    includes are known, and any ``{% include %}`` tag in the plan will
    raise :class:`TemplateError`.
    """
    return _instantiate_plan(row.plan, params, includes)


def instantiate_fixture(
    plan: dict[str, Any],
    params: dict[str, Any],
    *,
    includes: dict[str, str] | None = None,
) -> WorkFlow:
    """Same as :func:`instantiate_template` but takes a raw plan dict
    rather than a persisted ``WorkflowTemplateRow``.

    Used by callers that load fixtures directly from disk (no DB) — e.g.
    the ChatFlow engine, which materializes ``judge_pre`` / ``judge_post``
    inside ``_spawn_turn_node`` (sync, no AsyncSession available).
    """
    return _instantiate_plan(plan, params, includes)


def _instantiate_plan(
    plan: dict[str, Any],
    params: dict[str, Any],
    includes: dict[str, str] | None,
) -> WorkFlow:
    include_texts = includes or {}
    substituted = _walk_substitute(plan, params, include_texts)
    remapped = _remap_ids(substituted)
    return WorkFlow.model_validate(remapped)
