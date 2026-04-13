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

from pydantic import ValidationError

from agentloom.schemas.common import JudgeVariant, JudgeVerdict

_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json|JSON)?\s*\n?(?P<body>.*?)\n?\s*```\s*$", re.DOTALL
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


def _extract_json_object(text: str) -> str:
    """Best-effort: return the substring from the first '{' to the
    last '}' inclusive. Handles leading chit-chat like 'Here is the
    verdict:\\n{...}'."""
    stripped = _strip_code_fence(text.strip())
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
