"""Load YAML fixtures from ``fixtures/*.yaml`` into the
``workflow_templates`` table under ``workspace_id="__builtin__"``.

A fixture file is a dict with the following top-level shape:

.. code-block:: yaml

    builtin_id: plan         # stable identifier (filename-independent)
    name: "Plan"
    description: "..."
    params_schema:           # optional, advisory only for M10.1
      goal: {type: string, required: true}
    plan:                    # serialized WorkFlow payload
      description: ...
      nodes: [...]
      root_ids: [...]

Files whose stem starts with ``_`` are treated as **include fragments**
used by ``{% include 'name' %}`` substitution at instantiate time — they
are not themselves templates and carry a single ``text`` key.

The loader is called at app startup (see ``main.py`` wiring in M10.2) and
is idempotent: each fixture's ``(workspace_id, builtin_id)`` row is
upserted. It never touches user-workspace rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentloom.db.models.workflow_template import WorkflowTemplateRow
from agentloom.schemas.common import generate_node_id, utcnow

BUILTIN_WORKSPACE_ID = "__builtin__"

#: Directory that ships with the library — any ``*.yaml`` alongside this
#: module is picked up.
FIXTURES_DIR = Path(__file__).parent / "fixtures"


@dataclass(frozen=True, slots=True)
class FixtureData:
    """Parsed YAML content for one template fixture."""

    builtin_id: str
    name: str
    description: str
    params_schema: dict[str, Any] | None
    plan: dict[str, Any]


@dataclass(frozen=True, slots=True)
class IncludeFragment:
    """A ``_*.yaml`` file whose ``text`` body is spliced in by
    ``{% include 'name' %}`` at instantiate time."""

    name: str
    text: str


class FixtureParseError(ValueError):
    """Raised when a fixture YAML is malformed or missing required keys."""


def _parse_template_fixture(path: Path, raw: dict[str, Any]) -> FixtureData:
    required = {"builtin_id", "name", "plan"}
    missing = required - raw.keys()
    if missing:
        raise FixtureParseError(
            f"{path.name}: missing required keys {sorted(missing)}"
        )
    if not isinstance(raw["plan"], dict):
        raise FixtureParseError(f"{path.name}: 'plan' must be a mapping")
    return FixtureData(
        builtin_id=str(raw["builtin_id"]),
        name=str(raw["name"]),
        description=str(raw.get("description", "")),
        params_schema=raw.get("params_schema"),
        plan=raw["plan"],
    )


def _parse_include_fragment(path: Path, raw: dict[str, Any]) -> IncludeFragment:
    if "text" not in raw or not isinstance(raw["text"], str):
        raise FixtureParseError(
            f"{path.name}: include fragment must define string 'text'"
        )
    return IncludeFragment(name=path.stem, text=raw["text"])


def fragments_as_texts(
    fragments: dict[str, IncludeFragment],
) -> dict[str, str]:
    """Flatten ``{name: IncludeFragment}`` to ``{name: text}`` for the
    shape :func:`agentloom.templates.instantiate.instantiate_template`
    expects."""
    return {name: frag.text for name, frag in fragments.items()}


def load_fixtures(
    fixtures_dir: Path | None = None,
) -> tuple[list[FixtureData], dict[str, IncludeFragment]]:
    """Read all ``*.yaml`` files under *fixtures_dir* (default: shipped).

    Returns a tuple ``(templates, includes)`` where ``includes`` maps
    fragment name (filename stem with leading underscore preserved) to
    its :class:`IncludeFragment`. The caller can then hand ``includes``
    to :func:`instantiate_template`.

    File naming: ``_foo.yaml`` → include fragment named ``_foo``;
    anything else is a template fixture.
    """
    root = fixtures_dir or FIXTURES_DIR
    templates: list[FixtureData] = []
    includes: dict[str, IncludeFragment] = {}
    for path in sorted(root.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise FixtureParseError(f"{path.name}: root must be a mapping")
        if path.stem.startswith("_"):
            frag = _parse_include_fragment(path, raw)
            includes[frag.name] = frag
        else:
            templates.append(_parse_template_fixture(path, raw))
    _check_include_cycles(includes)
    return templates, includes


_INCLUDE_RE_SRC = r"\{%\s*include\s+'([^']+)'\s*%\}"


def _find_include_refs(text: str) -> list[str]:
    """Return names referenced by ``{% include 'name' %}`` tags."""
    import re

    return re.findall(_INCLUDE_RE_SRC, text)


def _check_include_cycles(includes: dict[str, IncludeFragment]) -> None:
    """DFS each fragment's include graph; raise on a cycle."""
    from agentloom.templates.instantiate import TemplateCycleError

    def visit(name: str, stack: list[str]) -> None:
        if name in stack:
            chain = " -> ".join(stack + [name])
            raise TemplateCycleError(f"include cycle: {chain}")
        if name not in includes:
            return  # unknown — surfaced later at instantiate time
        stack = [*stack, name]
        for ref in _find_include_refs(includes[name].text):
            visit(ref, stack)

    for name in includes:
        visit(name, [])


async def _ensure_builtin_workspace(session: AsyncSession) -> None:
    """Insert the ``__builtin__`` workspace row if missing.

    Self-heals in test environments where the conftest only seeds
    ``default``. In production this row is created by migration 0006.
    """
    from agentloom.db.models.tenancy import Workspace

    existing = await session.get(Workspace, BUILTIN_WORKSPACE_ID)
    if existing is None:
        session.add(Workspace(id=BUILTIN_WORKSPACE_ID, name="__builtin__"))
        await session.flush()


async def upsert_builtin_templates(
    session: AsyncSession,
    fixtures_dir: Path | None = None,
) -> list[WorkflowTemplateRow]:
    """Upsert every fixture under ``fixtures_dir`` as a ``__builtin__``
    row. Safe to call repeatedly.

    Update strategy: match by ``(workspace_id=__builtin__, builtin_id)``.
    If the row exists, overwrite ``name``, ``description``, ``plan``, and
    ``params_schema``. Otherwise insert a new row with a fresh id. We do
    **not** touch ``created_at`` on updates, so the original install
    timestamp is preserved.
    """
    await _ensure_builtin_workspace(session)
    templates, _includes = load_fixtures(fixtures_dir)
    rows: list[WorkflowTemplateRow] = []
    for fx in templates:
        stmt = select(WorkflowTemplateRow).where(
            WorkflowTemplateRow.workspace_id == BUILTIN_WORKSPACE_ID,
            WorkflowTemplateRow.builtin_id == fx.builtin_id,
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = WorkflowTemplateRow(
                id=generate_node_id(),
                workspace_id=BUILTIN_WORKSPACE_ID,
                owner_id=None,
                builtin_id=fx.builtin_id,
                name=fx.name,
                description=fx.description,
                plan=fx.plan,
                params_schema=fx.params_schema,
            )
            session.add(row)
        else:
            row.name = fx.name
            row.description = fx.description
            row.plan = fx.plan
            row.params_schema = fx.params_schema
            row.updated_at = utcnow()
        rows.append(row)
    await session.flush()
    return rows
