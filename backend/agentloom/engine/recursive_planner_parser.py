"""Parse the recursive-planner WorkNode's JSON output.

The planner template (``templates/fixtures/planner.yaml``) prompts the
LLM to emit one of three shapes:

- ``atomic``:     a single worker brief — the level can be done in one
                  llm_call or one tool_call.
- ``decompose``:  N sub-tasks, each carrying its own trio. The
                  orchestrator spawns one ``sub_agent_delegation`` per
                  sub-task.
- ``infeasible``: the planner can find no viable decomposition; the
                  orchestrator routes this to ``judge_post`` as a halt.

Like the judge / legacy-plan parsers, real-world models sometimes wrap
the JSON in a Markdown fence or prepend chit-chat — we strip that
defensively before validation. Structural errors raise
:class:`PlannerParseError`; the engine treats that as a planner failure
(same fallback shape as a judge that can't be parsed).
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from agentloom.schemas.common import StepKind

_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json|JSON)?\s*\n?(?P<body>.*?)\n?\s*```\s*$", re.DOTALL
)


class PlannerParseError(ValueError):
    """Raised when a planner WorkNode's raw output cannot be parsed."""


class AtomicBrief(BaseModel):
    """The realised trio for a single worker the planner declared atomic."""

    step_kind: StepKind
    description: str
    inputs: str = ""
    expected_outcome: str
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None


class SubTask(BaseModel):
    """One sub-WorkFlow trio in a ``decompose`` plan.

    ``after`` is the list of 0-indexed positions of sub-tasks that must
    complete before this one runs; an empty list means the sub-task can
    start in parallel with any other root sibling.
    """

    description: str
    inputs: str = ""
    expected_outcome: str
    after: list[int] = Field(default_factory=list)


class RecursivePlannerOutput(BaseModel):
    """Typed view of the planner's JSON. See planner.yaml for the schema
    the LLM is prompted to emit.

    ``reasoning`` is listed first so json_schema-enforced models are
    nudged to think before committing to a mode. It's optional — thinking-
    channel models (Claude extended thinking, o-series, Qwen3 reasoning)
    may leave it null because their reasoning is carried on a private
    channel; direct-output models tend to populate it. Either way the
    field gives us a parseable, user-visible trace of the decomposition
    rationale that's otherwise lost.
    """

    reasoning: str | None = None
    mode: Literal["atomic", "decompose", "infeasible"]
    atomic: AtomicBrief | None = None
    subtasks: list[SubTask] | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_shape(self) -> "RecursivePlannerOutput":
        # Cross-field consistency: the discriminator must match the
        # populated payload, otherwise the orchestrator has no way to
        # know which branch the planner actually intended.
        if self.mode == "atomic":
            if self.atomic is None:
                raise ValueError("mode=atomic requires the 'atomic' field")
        elif self.mode == "decompose":
            if not self.subtasks:
                raise ValueError("mode=decompose requires non-empty 'subtasks'")
            self._validate_after_graph()
        elif self.mode == "infeasible":
            if not self.reason:
                raise ValueError("mode=infeasible requires a 'reason'")
        return self

    def _validate_after_graph(self) -> None:
        """Sanity-check the ``after`` references in a decompose plan.

        We catch (a) out-of-range indices and (b) self-references here;
        cycle detection is left to the orchestrator that actually
        topologically sorts the sub-tasks (it has to walk the graph
        anyway to spawn them in dependency order).
        """
        n = len(self.subtasks or [])
        for i, st in enumerate(self.subtasks or []):
            for ref in st.after:
                if ref == i:
                    raise ValueError(
                        f"subtask[{i}] cannot list itself in 'after'"
                    )
                if ref < 0 or ref >= n:
                    raise ValueError(
                        f"subtask[{i}].after references out-of-range index {ref}"
                    )


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


def parse_recursive_planner_output(raw: str) -> RecursivePlannerOutput:
    """Parse *raw* assistant output into a :class:`RecursivePlannerOutput`.

    Raises :class:`PlannerParseError` on JSON parse failure, on Pydantic
    validation failure, or on cross-field inconsistency (e.g. ``mode:
    "atomic"`` with no ``atomic`` payload).
    """
    payload = _extract_json_object(raw)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PlannerParseError(f"planner output is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PlannerParseError(
            f"planner output must be a JSON object, got {type(data).__name__}"
        )
    try:
        return RecursivePlannerOutput.model_validate(data)
    except ValidationError as exc:
        raise PlannerParseError(
            f"planner output failed schema validation: {exc}"
        ) from exc
