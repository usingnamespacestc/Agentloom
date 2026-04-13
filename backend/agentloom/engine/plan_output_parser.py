"""Parse raw LLM output from a plan step into a structured
:class:`ParsedPlan`.

The planner is prompted (by ``templates/fixtures/plan.yaml``) to return
JSON describing the nodes to materialize as dashed/PLANNED WorkNodes.
Like judges, real models sometimes wrap the JSON in a Markdown fence
or prepend chit-chat; we strip that defensively.

The parsed structure is intentionally minimal and decoupled from
:class:`WorkFlowNode`: it only carries the fields the materializer
needs. Validation of the plan against user-placed keyframes happens
in :mod:`agentloom.engine.keyframe_validator` — this module is parse
only.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from agentloom.schemas.common import NodeId, StepKind

_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json|JSON)?\s*\n?(?P<body>.*?)\n?\s*```\s*$", re.DOTALL
)


class PlanParseError(ValueError):
    """Raised when a plan step's raw output cannot be parsed into a
    :class:`ParsedPlan`."""


class PlanNodeSpec(BaseModel):
    """One node the planner wants to materialize.

    ``id`` is the planner's chosen id — it may collide with an existing
    keyframe id (that is how the planner references a keyframe it must
    keep) or be a freshly coined id for a new dashed node."""

    id: NodeId
    step_kind: StepKind
    description: str = ""
    inputs: str | None = None
    expected_outcome: str | None = None
    parent_ids: list[NodeId] = Field(default_factory=list)
    # tool_call fields
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None


class ParsedPlan(BaseModel):
    """A planner's proposal: a list of node specs that together form a DAG."""

    nodes: list[PlanNodeSpec] = Field(default_factory=list)

    def node_ids(self) -> set[NodeId]:
        return {n.id for n in self.nodes}

    def get(self, node_id: NodeId) -> PlanNodeSpec | None:
        return next((n for n in self.nodes if n.id == node_id), None)


def _strip_code_fence(text: str) -> str:
    m = _CODE_FENCE_RE.match(text)
    return m.group("body") if m else text


def _extract_json_object(text: str) -> str:
    stripped = _strip_code_fence(text.strip())
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last < first:
        return stripped  # let json.loads surface the error
    return stripped[first : last + 1]


def parse_plan(raw: str) -> ParsedPlan:
    """Parse *raw* assistant output into a :class:`ParsedPlan`.

    Raises :class:`PlanParseError` on JSON parse failure, on Pydantic
    validation failure, or on structural problems (missing ``nodes``
    key, duplicate ids, parent ids that don't resolve within the plan).
    """
    payload = _extract_json_object(raw)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PlanParseError(f"plan output is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PlanParseError(
            f"plan output must be a JSON object, got {type(data).__name__}"
        )
    try:
        plan = ParsedPlan.model_validate(data)
    except ValidationError as exc:
        raise PlanParseError(f"plan output failed schema validation: {exc}") from exc

    ids = [n.id for n in plan.nodes]
    if len(ids) != len(set(ids)):
        raise PlanParseError("plan contains duplicate node ids")

    known = set(ids)
    for n in plan.nodes:
        for p in n.parent_ids:
            if p not in known:
                raise PlanParseError(
                    f"plan node {n.id!r} references unknown parent {p!r}"
                )
    return plan
