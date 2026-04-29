"""Parse raw LLM output from a ``judge_call`` WorkNode into a
structured :class:`agentloom.schemas.common.JudgeVerdict`.

Judges are instructed (by the shipped templates in
``agentloom/templates/fixtures/judge_*.yaml``) to return JSON with a
specific schema. Real-world models sometimes wrap the JSON in a
Markdown code fence or prepend chit-chat — we strip that defensively.

Per-variant sanity:

- ``pre``:     requires a ``feasibility`` value.
- ``during``:  requires a ``during_verdict`` value.
- ``post``:    requires a ``post_verdict`` value.

Anything else (missing or malformed) raises :class:`JudgeParseError`.
The engine treats that as a failed judge_call and keeps the raw output
on the node's ``output_message`` so the user can re-run or inspect.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from agentloom.providers.types import ToolDefinition
from agentloom.schemas.common import JudgeVariant, JudgeVerdict

_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json|JSON)?\s*\n?(?P<body>.*?)\n?\s*```\s*$", re.DOTALL
)

#: Complete ``<think>...</think>`` pairs. Non-greedy so multiple think
#: blocks don't collapse into one super-block. DOTALL because reasoning
#: spans newlines. Case-insensitive because some llama.cpp reasoning
#: builds emit ``<Think>`` or ``<THINK>``.
_THINK_PAIR_RE = re.compile(
    r"<think\b[^>]*>.*?</think>\s*", re.DOTALL | re.IGNORECASE
)
#: Orphan open tag with no matching close — happens when ``max_tokens``
#: cuts the response off mid-thought. Everything from the open tag to
#: end-of-string is dropped: the JSON never arrived anyway, and if some
#: garbage follows the orphan ``<think>`` we don't want its braces
#: fooling the first-``{``/last-``}`` extraction below.
_THINK_OPEN_RE = re.compile(
    r"<think\b[^>]*>.*", re.DOTALL | re.IGNORECASE
)

# JudgeVerdict fields whose Python type is a list. Models sometimes emit
# ``"redo_targets": null`` (etc.) instead of ``[]``; pydantic rejects that
# because the fields are typed ``list[...]``, not ``list[...] | None``.
# We coerce those nulls to ``[]`` at the parse layer rather than widening
# the schema — the engine semantics treat "absent" and "empty" the same.
_LIST_FIELDS = (
    "blockers",
    "missing_inputs",
    "critiques",
    "issues",
    "redo_targets",
    "capability_escalation",
    "recon_plan",
)


class JudgeParseError(ValueError):
    """Raised when a judge_call's raw output cannot be parsed into a
    :class:`JudgeVerdict` matching its declared variant."""


def _strip_code_fence(text: str) -> str:
    """If *text* is wrapped in a single Markdown code fence, return the
    fence body. Otherwise return *text* untouched.

    We only unwrap the *outermost* fence; nested code blocks inside the
    JSON (e.g. inside an "evidence" string) are left alone."""
    m = _CODE_FENCE_RE.match(text)
    return m.group("body") if m else text


def _strip_think_tags(text: str) -> str:
    """Remove reasoning-channel ``<think>...</think>`` blocks from
    *text*. Two passes:

    1. Strip every complete pair (non-greedy, so multiple blocks stay
       separate).
    2. Strip any orphan-open tag — ``<think>`` with no matching close,
       which happens when the model's ``max_tokens`` cut the response
       before the close tag or before the JSON arrived.

    Protocol-level reasoning channels (Anthropic thinking blocks,
    DeepSeek ``reasoning_content``, Volcengine ``thinking_content``)
    are already pulled into ``message.extras["thinking"]`` by the
    provider adapters, so this helper only fires on models that emit
    inline ``<think>`` tags in the content field (many llama.cpp /
    ggml-hosted reasoning models: Qwen3, R1-distill, GLM).
    """
    text = _THINK_PAIR_RE.sub("", text)
    text = _THINK_OPEN_RE.sub("", text)
    return text


def _extract_json_object(text: str) -> str:
    """Best-effort: return the substring from the first '{' to the
    last '}' inclusive. Handles leading chit-chat like 'Here is the
    verdict:\\n{...}' and inline ``<think>...</think>`` reasoning tags
    that some locally-served reasoning models emit before the JSON."""
    stripped = _strip_think_tags(_strip_code_fence(text.strip()))
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last < first:
        return stripped  # let json.loads surface the error
    return stripped[first : last + 1]


def parse_judge_verdict(raw: str, variant: JudgeVariant) -> JudgeVerdict:
    """Parse *raw* assistant output into a :class:`JudgeVerdict`.

    Raises :class:`JudgeParseError` on JSON parse failure, on Pydantic
    validation failure, or on missing the variant-required discriminator
    field.
    """
    payload = _extract_json_object(raw)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise JudgeParseError(f"judge output is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise JudgeParseError(
            f"judge output must be a JSON object, got {type(data).__name__}"
        )
    for key in _LIST_FIELDS:
        if data.get(key) is None and key in data:
            data[key] = []
    try:
        verdict = JudgeVerdict.model_validate(data)
    except ValidationError as exc:
        raise JudgeParseError(f"judge output failed schema validation: {exc}") from exc

    # Variant-required discriminator: a pre judge must tell us feasibility,
    # etc. Otherwise the engine has nothing to branch on.
    match variant:
        case JudgeVariant.PRE:
            if verdict.feasibility is None:
                raise JudgeParseError(
                    "judge_pre output missing required 'feasibility'"
                )
        case JudgeVariant.DURING:
            if verdict.during_verdict is None:
                raise JudgeParseError(
                    "judge_during output missing required 'during_verdict'"
                )
        case JudgeVariant.POST:
            if verdict.post_verdict is None:
                raise JudgeParseError(
                    "judge_post output missing required 'post_verdict'"
                )
    return verdict


# ------------------------------------------------------------------ tool_use

_VARIANT_SCHEMAS: dict[JudgeVariant, dict[str, Any]] = {
    JudgeVariant.PRE: {
        "type": "object",
        "properties": {
            "extracted_description": {"type": "string"},
            "extracted_inputs": {"type": "string"},
            "extracted_expected_outcome": {"type": "string"},
            "feasibility": {
                "type": "string",
                "enum": ["ok", "risky", "infeasible"],
            },
            "blockers": {
                "type": "array",
                "items": {"type": "string"},
            },
            "missing_inputs": {
                "type": "array",
                "items": {"type": "string"},
            },
            "extracted_capabilities": {
                "type": "array",
                "items": {"type": "string"},
            },
            "extracted_inheritable_tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "M7.5 capability model: registry tool names this "
                    "WorkFlow's planner is permitted to authorize for "
                    "subtasks. Pick verbatim from the catalog block in "
                    "the judge_pre prompt."
                ),
            },
            "recon_plan": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "args": {"type": "object"},
                    },
                    "required": ["name"],
                },
                "description": (
                    "M7.5 PR 7 cognitive ReAct DAG: read-only tool "
                    "calls the engine should run before judge_pre's "
                    "verdict is final. Use only when feasibility "
                    "genuinely depends on what those tools would "
                    "find — leave empty when you can decide from the "
                    "transcript alone."
                ),
            },
        },
        "required": [
            "extracted_description",
            "extracted_inputs",
            "extracted_expected_outcome",
            "feasibility",
        ],
    },
    JudgeVariant.DURING: {
        "type": "object",
        "properties": {
            "during_verdict": {
                "type": "string",
                "enum": ["continue", "revise", "halt"],
            },
            "critiques": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "issue": {"type": "string"},
                        "severity": {
                            "type": "string",
                            "enum": ["blocker", "concern", "nit"],
                        },
                        "evidence": {"type": "string"},
                    },
                    "required": ["issue"],
                },
            },
            "capability_escalation": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "M7.5 capability_request bubble-up: registry tool "
                    "names the worker draft asked for via "
                    "<capability_request> markers but couldn't access. "
                    "Surfaces the gap to the orchestrator without "
                    "halting the WorkFlow."
                ),
            },
        },
        "required": ["during_verdict"],
    },
    JudgeVariant.POST: {
        "type": "object",
        "properties": {
            "post_verdict": {
                "type": "string",
                "enum": ["accept", "retry", "fail"],
            },
            "user_message": {"type": "string"},
            "merged_response": {"type": "string"},
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "location": {"type": "string"},
                        "expected": {"type": "string"},
                        "actual": {"type": "string"},
                    },
                    "required": ["location", "expected", "actual"],
                },
            },
            "redo_targets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "node_id": {"type": "string"},
                        "critique": {"type": "string"},
                    },
                    "required": ["node_id", "critique"],
                },
            },
            "capability_escalation": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "M7.5 capability_request bubble-up: registry tool "
                    "names worker drafts asked for and judge_post is "
                    "surfacing to the orchestrator. Same field as the "
                    "DURING variant — populated by whichever judge "
                    "first sees the worker marker."
                ),
            },
            "recon_plan": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "args": {"type": "object"},
                    },
                    "required": ["name"],
                },
                "description": (
                    "M7.5 PR 7 sub-task 3: cognitive ReAct DAG for "
                    "judge_post. When deciding accept/retry/fail "
                    "depends on verifying real state (worker claimed "
                    "a mutation, accept hinges on confirming it), "
                    "emit read-only tool calls here instead of "
                    "committing to a verdict. Engine runs them, then "
                    "re-runs judge_post with the results in context. "
                    "One round only — recursion is fused. Leave "
                    "empty when the transcript already shows enough "
                    "to decide."
                ),
            },
        },
        "required": ["post_verdict"],
    },
}

_VARIANT_DESCRIPTIONS: dict[JudgeVariant, str] = {
    JudgeVariant.PRE: "Submit the pre-judge feasibility verdict.",
    JudgeVariant.DURING: "Submit the during-judge monitoring verdict.",
    JudgeVariant.POST: "Submit the post-judge acceptance verdict.",
}


def judge_verdict_tool_def(variant: JudgeVariant) -> ToolDefinition:
    """Return a ``ToolDefinition`` that forces the model to produce a
    structured judge verdict via tool_use instead of free-text JSON."""
    return ToolDefinition(
        name="judge_verdict",
        description=_VARIANT_DESCRIPTIONS[variant],
        parameters=_VARIANT_SCHEMAS[variant],
    )


def judge_verdict_json_schema(variant: JudgeVariant) -> dict[str, Any]:
    """Return the JSON schema for *variant*'s expected output.

    Used by the engine to pass a ``response_format=json_schema`` shape
    through to providers that support content-level JSON enforcement
    alongside tool_use. Having both belt (tool_choice) and braces
    (response_format) guards against models that ignore one but honor
    the other.
    """
    schema = dict(_VARIANT_SCHEMAS[variant])
    # response_format=json_schema needs a ``title`` to derive the output
    # name OpenAI uses. Match the tool name so logs are greppable.
    schema.setdefault("title", "judge_verdict")
    return schema


def parse_judge_from_tool_args(
    args: dict[str, Any], variant: JudgeVariant
) -> JudgeVerdict:
    """Parse tool_use arguments into a :class:`JudgeVerdict`.

    Same validation as :func:`parse_judge_verdict` but skips the JSON
    extraction step since the arguments are already a parsed dict.

    Special case: ``{"_raw": <str>}`` is a sentinel the provider adapter
    emits when the raw tool_call arguments string could not be parsed as
    JSON (observed with ark-code-latest on Chinese payloads — literal
    inner ``"`` characters left unescaped). Surface the raw body in the
    error so the retry path's prompt can echo it back with an explicit
    escape hint; silently treating it as empty args buries the evidence.
    """
    if set(args.keys()) == {"_raw"} and isinstance(args["_raw"], str):
        raw_preview = args["_raw"][:400]
        raise JudgeParseError(
            f"judge tool_use arguments are not valid JSON (adapter "
            f"sentinel _raw): {raw_preview!r}"
        )
    for key in _LIST_FIELDS:
        if args.get(key) is None and key in args:
            args[key] = []
    try:
        verdict = JudgeVerdict.model_validate(args)
    except ValidationError as exc:
        raise JudgeParseError(f"judge tool_use args failed validation: {exc}") from exc

    match variant:
        case JudgeVariant.PRE:
            if verdict.feasibility is None:
                raise JudgeParseError(
                    "judge_pre tool_use missing required 'feasibility'"
                )
        case JudgeVariant.DURING:
            if verdict.during_verdict is None:
                raise JudgeParseError(
                    "judge_during tool_use missing required 'during_verdict'"
                )
        case JudgeVariant.POST:
            if verdict.post_verdict is None:
                raise JudgeParseError(
                    "judge_post tool_use missing required 'post_verdict'"
                )
    return verdict
