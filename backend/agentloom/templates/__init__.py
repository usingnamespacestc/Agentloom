"""System workflow template engine (ADR-019).

Shipped system workflows are stored as YAML fixtures under
``fixtures/`` and loaded into the ``workflow_templates`` table at startup
under ``workspace_id="__builtin__"``. User workspaces can shadow any
fixture by writing a row with a matching ``builtin_id`` — lookup via
:func:`resolve_template` prefers the user's override.

Two-step pipeline:

- :mod:`agentloom.templates.loader` reads YAML fixtures off disk and
  upserts the ``__builtin__`` rows.
- :mod:`agentloom.templates.instantiate` takes a resolved row + params
  and produces a concrete :class:`agentloom.schemas.WorkFlow` with fresh
  node ids, Jinja-style ``{{ param }}`` substituted, and
  ``{% include 'other_id' %}`` fragments pulled in (with cycle
  detection).
"""

from agentloom.templates.instantiate import (
    BUILTIN_WORKSPACE_ID,
    TemplateCycleError,
    TemplateError,
    TemplateNotFoundError,
    UnboundParamError,
    instantiate_template,
    resolve_template,
)
from agentloom.templates.loader import (
    FixtureData,
    FixtureParseError,
    IncludeFragment,
    fragments_as_texts,
    load_fixtures,
    upsert_builtin_templates,
)

__all__ = [
    "BUILTIN_WORKSPACE_ID",
    "FixtureData",
    "FixtureParseError",
    "IncludeFragment",
    "TemplateCycleError",
    "TemplateError",
    "TemplateNotFoundError",
    "UnboundParamError",
    "fragments_as_texts",
    "instantiate_template",
    "load_fixtures",
    "resolve_template",
    "upsert_builtin_templates",
]
