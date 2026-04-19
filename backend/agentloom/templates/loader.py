"""Load YAML fixtures from ``fixtures/<language>/*.yaml`` into the
``workflow_templates`` table under ``workspace_id="__builtin__"``.

Layout::

    fixtures/
        en-US/planner.yaml    # English authored version
        en-US/_critique_base.yaml
        zh-CN/planner.yaml    # Chinese authored version

Each language's templates are upserted with ``builtin_id`` suffixed by
``@<language>`` — e.g. the English planner stores as ``planner@en-US``.
The engine's template lookups append the suffix derived from the
workspace's current ``language`` setting, falling back to ``@en-US``
when a translation hasn't been authored yet.

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

#: Directory that ships with the library. Each immediate subdirectory
#: is a language tag (``en-US``, ``zh-CN``, …) whose ``*.yaml`` files
#: are template fixtures or include fragments for that language.
FIXTURES_DIR = Path(__file__).parent / "fixtures"

#: Canonical fallback language. When a workspace requests a language
#: for which no fixture exists, the loader's in-memory dict returns
#: the entry for this language instead.
DEFAULT_LANGUAGE = "en-US"


def suffixed_builtin_id(builtin_id: str, language: str) -> str:
    """Return the DB-side ``builtin_id`` key for a per-language fixture,
    e.g. ``planner`` + ``zh-CN`` → ``planner@zh-CN``. Kept in one place
    so resolver / loader / engine agree on the scheme."""
    return f"{builtin_id}@{language}"


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
    *,
    language: str = DEFAULT_LANGUAGE,
) -> tuple[list[FixtureData], dict[str, IncludeFragment]]:
    """Read ``*.yaml`` files under ``fixtures_dir/<language>`` (default:
    shipped en-US). For a different *language*, falls back to
    :data:`DEFAULT_LANGUAGE` for any fixture or include fragment that
    has no translation in *language* yet.

    Returns a tuple ``(templates, includes)`` where ``includes`` maps
    fragment name (filename stem with leading underscore preserved) to
    its :class:`IncludeFragment`. The caller can then hand ``includes``
    to :func:`instantiate_template`.

    File naming: ``_foo.yaml`` → include fragment named ``_foo``;
    anything else is a template fixture.
    """
    root = fixtures_dir or FIXTURES_DIR
    fallback_tpls, fallback_incs = _read_language_dir(root / DEFAULT_LANGUAGE)
    if language == DEFAULT_LANGUAGE:
        _check_include_cycles(fallback_incs)
        return fallback_tpls, fallback_incs

    lang_tpls, lang_incs = _read_language_dir(root / language)
    tpl_by_id: dict[str, FixtureData] = {fx.builtin_id: fx for fx in fallback_tpls}
    tpl_by_id.update({fx.builtin_id: fx for fx in lang_tpls})
    inc_by_name: dict[str, IncludeFragment] = dict(fallback_incs)
    inc_by_name.update(lang_incs)
    merged_tpls = list(tpl_by_id.values())
    _check_include_cycles(inc_by_name)
    return merged_tpls, inc_by_name


def _read_language_dir(
    lang_dir: Path,
) -> tuple[list[FixtureData], dict[str, IncludeFragment]]:
    """Read a single ``fixtures/<lang>/`` directory. Missing directory
    yields empty results so callers can merge with a fallback."""
    templates: list[FixtureData] = []
    includes: dict[str, IncludeFragment] = {}
    if not lang_dir.is_dir():
        return templates, includes
    for path in sorted(lang_dir.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise FixtureParseError(f"{path.name}: root must be a mapping")
        if path.stem.startswith("_"):
            frag = _parse_include_fragment(path, raw)
            includes[frag.name] = frag
        else:
            templates.append(_parse_template_fixture(path, raw))
    return templates, includes


def list_available_languages(
    fixtures_dir: Path | None = None,
) -> list[str]:
    """Return the set of language subdirectories present under
    *fixtures_dir*, sorted. Used by the loader's DB upsert to know
    which languages to materialize."""
    root = fixtures_dir or FIXTURES_DIR
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


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
    """Upsert every fixture under ``fixtures_dir/<language>/`` as a
    ``__builtin__`` row, one row per ``(fixture, language)`` pair. The
    stored ``builtin_id`` is suffixed via :func:`suffixed_builtin_id`
    (e.g. ``planner@zh-CN``). Safe to call repeatedly.

    Update strategy: match by ``(workspace_id=__builtin__, builtin_id)``.
    If the row exists, overwrite ``name``, ``description``, ``plan``, and
    ``params_schema``. Otherwise insert a new row with a fresh id. We do
    **not** touch ``created_at`` on updates, so the original install
    timestamp is preserved.
    """
    await _ensure_builtin_workspace(session)
    rows: list[WorkflowTemplateRow] = []
    for language in list_available_languages(fixtures_dir):
        templates, _includes = load_fixtures(fixtures_dir, language=language)
        # ``load_fixtures`` merges the target language on top of the
        # fallback, so every language ends up with a full set of
        # ``builtin_id``s — untranslated ones just point at the en-US
        # plan. This lets callers query ``<id>@<lang>`` directly
        # without extra fallback logic.
        for fx in templates:
            suffixed = suffixed_builtin_id(fx.builtin_id, language)
            stmt = select(WorkflowTemplateRow).where(
                WorkflowTemplateRow.workspace_id == BUILTIN_WORKSPACE_ID,
                WorkflowTemplateRow.builtin_id == suffixed,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                row = WorkflowTemplateRow(
                    id=generate_node_id(),
                    workspace_id=BUILTIN_WORKSPACE_ID,
                    owner_id=None,
                    builtin_id=suffixed,
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
