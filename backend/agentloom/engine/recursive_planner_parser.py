"""Parse the recursive-planner WorkNode's JSON output.

The planner template (``templates/fixtures/planner.yaml``) prompts the
LLM to emit one of three shapes:

- ``atomic``:     a single worker brief — the level can be done in one
                  llm_call or one tool_call. Carries its own trio because
                  the worker template consumes it verbatim, without a
                  sub-WorkFlow in between.
- ``decompose``:  N sub-tasks, each a natural-language description of
                  what that sub-agent should do. The orchestrator spawns
                  one ``sub_agent_delegation`` per sub-task; each sub-
                  WorkFlow's own ``judge_pre`` reads the description and
                  distills the trio. Planner does NOT write trio for
                  sub-tasks — that's judge_pre's job.
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
# See judge_parser._strip_think_tags for the rationale; same regex pair.
_THINK_PAIR_RE = re.compile(
    r"<think\b[^>]*>.*?</think>\s*", re.DOTALL | re.IGNORECASE
)
_THINK_OPEN_RE = re.compile(
    r"<think\b[^>]*>.*", re.DOTALL | re.IGNORECASE
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
    """One sub-WorkFlow task in a ``decompose`` plan.

    Just the natural-language description plus the ``after`` dependency
    graph. Trio extraction is the downstream sub-WorkFlow's judge_pre's
    job — see ``_build_sub_workflow_for_subtask``.

    ``after`` is the list of 0-indexed positions of sub-tasks that must
    complete before this one runs; an empty list means the sub-task can
    start in parallel with any other root sibling.
    """

    description: str
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


def _strip_think_tags(text: str) -> str:
    """See :func:`agentloom.engine.judge_parser._strip_think_tags`."""
    text = _THINK_PAIR_RE.sub("", text)
    text = _THINK_OPEN_RE.sub("", text)
    return text


def _extract_json_object(text: str) -> str:
    stripped = _strip_think_tags(_strip_code_fence(text.strip()))
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last < first:
        return stripped  # let json.loads surface the error
    return stripped[first : last + 1]


def planner_grammar_schema() -> dict[str, Any]:
    """JSON Schema for ``response_format`` enforcement at decoder level.

    Distinct from ``RecursivePlannerOutput.model_json_schema()``: the
    Pydantic-derived schema only marks ``mode`` required and treats
    ``atomic`` / ``subtasks`` / ``reason`` as independently optional —
    the cross-field constraint ("if mode=atomic, atomic body must be
    populated") lives in the Pydantic ``@model_validator(mode="after")``
    hook, which is Python runtime code, not JSON Schema. Provider-side
    grammar engines (llama.cpp ``response_format=json_schema``,
    OpenAI structured outputs) honor only the static JSON Schema, so
    feeding them the Pydantic-derived shape lets weak models output
    ``{"mode": "atomic"}`` with no ``atomic`` body and pass the
    decoder check.

    Discriminated ``oneOf`` keyed on ``mode`` makes the cross-field
    constraint statically expressible: each branch fixes ``mode`` to
    a constant and lists the body field as required. llama.cpp's
    grammar engine compiles this into a token-level constraint that
    physically prevents the missing-body class of error.

    Used by ``WorkflowEngine._run_llm_call`` for planner WorkNodes.
    Other callers (parsing, tests, debugging) keep using
    ``RecursivePlannerOutput.model_json_schema()`` — the runtime
    Pydantic validator catches anything that slips through.
    """
    pyd_schema = RecursivePlannerOutput.model_json_schema()
    defs = pyd_schema.get("$defs", {})
    return {
        "title": "RecursivePlannerOutput",
        "$defs": defs,
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "reasoning": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                    },
                    "mode": {"const": "atomic"},
                    "atomic": {"$ref": "#/$defs/AtomicBrief"},
                },
                "required": ["mode", "atomic"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "reasoning": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                    },
                    "mode": {"const": "decompose"},
                    "subtasks": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/SubTask"},
                        "minItems": 1,
                    },
                },
                "required": ["mode", "subtasks"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "reasoning": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                    },
                    "mode": {"const": "infeasible"},
                    "reason": {"type": "string", "minLength": 1},
                },
                "required": ["mode", "reason"],
                "additionalProperties": False,
            },
        ],
    }


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


# ------------------------------------------------------------------ tool_use


#: Tool name the planner is pinned to via ``forced_tool_name`` when the
#: M7.5 PR 6 tool-use path runs. Mirrors ``judge_verdict`` for symmetry
#: — engine and parser key on this string, the LLM sees it via
#: ``tools=`` slot.
SUBMIT_PLAN_TOOL_NAME = "submit_plan"


def submit_plan_tool_def() -> dict[str, Any]:
    """Return a ToolDefinition-shaped dict that forces the planner to
    emit its decision via tool_use instead of free-text JSON.

    Mirrors the judge path: one tool, discriminated body. The
    ``parameters`` schema is the same ``oneOf`` shape that
    :func:`planner_grammar_schema` ships, so a model that does both
    tool_use and ``response_format=json_schema`` (volcengine /
    openai_chat under ``_RESPONSE_FORMAT_COEXISTS_WITH_TOOLS``) gets
    belt-and-suspenders enforcement: the tool_choice pin guarantees
    the structured call lands, and the json_schema double-checks the
    body shape.

    Why a single tool instead of ``[atomic, decompose, infeasible]``:
    keeps the engine free of cross-provider ``tool_choice="any"``
    plumbing (Anthropic, OpenAI, volcengine all spell it differently)
    and matches the existing ``judge_verdict`` precedent. The
    discriminated ``oneOf`` body is just as expressive — the model
    picks ``mode`` and the matching branch's required fields are
    enforced by the same constraint engine.
    """
    return {
        "name": SUBMIT_PLAN_TOOL_NAME,
        "description": (
            "Submit the planner's decomposition decision: atomic single-"
            "worker brief, decompose into N sub-tasks, or infeasible "
            "with a reason."
        ),
        "parameters": planner_grammar_schema(),
    }


def parse_planner_from_tool_args(args: dict[str, Any]) -> RecursivePlannerOutput:
    """Parse ``tool_use.arguments`` into a :class:`RecursivePlannerOutput`.

    Symmetric counterpart to :func:`parse_judge_from_tool_args` —
    skips the JSON-extraction step (arguments arrive as a parsed
    dict already) and runs the same Pydantic validation as the
    free-text path.

    Special case: ``{"_raw": <str>}`` is a sentinel some adapters
    emit when ``tool_call.arguments`` couldn't be parsed as JSON
    (observed with ark-code-latest on Chinese payloads — literal
    inner ``"`` left unescaped). Surface the raw body so the
    caller's retry path can echo it back to the model with an
    explicit escape hint instead of treating empty args as
    "atomic with no body".
    """
    if set(args.keys()) == {"_raw"} and isinstance(args["_raw"], str):
        raw_preview = args["_raw"][:400]
        raise PlannerParseError(
            f"planner tool_use arguments are not valid JSON (adapter "
            f"sentinel _raw): {raw_preview!r}"
        )
    try:
        return RecursivePlannerOutput.model_validate(args)
    except ValidationError as exc:
        raise PlannerParseError(
            f"planner tool_use args failed validation: {exc}"
        ) from exc
