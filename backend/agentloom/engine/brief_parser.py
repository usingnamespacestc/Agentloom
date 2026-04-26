"""Parse a brief WorkNode's structured output into description + tags.

The pre-2026-04-25 brief fixtures emitted free-form prose; the new
fixtures emit a JSON object ``{description, produced_tags,
consumed_tags}`` so MemoryBoard's logical-index axis (Karpathy
llm-wiki concept link graph) gets populated alongside the prose.

Strict path: when the provider supports ``response_format=json_schema``,
the engine threads :func:`brief_grammar_schema` to the wire and the
LLM is decoder-constrained to emit valid JSON. The grammar enforces
shape only — tag naming convention (lowercase snake_case + status
suffix syntax) is a prompt-level rule, not a schema-level one, so
fixtures still need the rule in their system prompts.

Lenient path: if the provider doesn't enforce schema (anthropic,
older configs, network blip) the model may emit prose or
slightly-malformed JSON. ``parse_brief_output`` recovers gracefully:

- Strip markdown fences;
- Try strict JSON parse;
- Fall back to first ``{...}`` block;
- Last resort treat the whole text as ``description`` with empty tags.

Tag normalization is also applied at parse time:
- lowercased + stripped;
- duplicates dropped (preserving first occurrence);
- entries that fail the ``^[a-z][a-z0-9_]*$`` shape are dropped (no
  uppercase, no whitespace, no hyphens, no dots — same convention
  the fixtures pin in their system prompts).
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

#: Tag shape: starts with a-z, then [a-z0-9_]. Underscore convention
#: chosen 2026-04-25 (vs kebab-case) so tags survive raw insertion in
#: SQL ``LIKE`` / ``?`` operators without escaping concerns.
_TAG_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: Hard ceiling on tag count emitted into a single brief, even before
#: per-chatflow limits apply. Defends against a runaway brief that
#: tries to flood produced_tags with 100 entries.
_TAG_HARD_CEILING = 30


class BriefOutput(BaseModel):
    """Typed view of one brief WorkNode's emitted JSON."""

    description: str = Field(..., min_length=1)
    produced_tags: list[str] = Field(default_factory=list)
    consumed_tags: list[str] = Field(default_factory=list)

    @field_validator("produced_tags", "consumed_tags", mode="after")
    @classmethod
    def _normalize_tags(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for raw in v:
            if not isinstance(raw, str):
                continue
            tag = raw.strip().lower()
            if not tag or not _TAG_RE.match(tag):
                continue
            if tag in seen:
                continue
            seen.add(tag)
            out.append(tag)
            if len(out) >= _TAG_HARD_CEILING:
                break
        return out


def brief_grammar_schema() -> dict[str, Any]:
    """JSON Schema for ``response_format`` enforcement on brief calls.

    Only structural — naming convention is enforced at the prompt
    layer + at parse time via :class:`BriefOutput`'s ``_normalize_tags``
    validator. Combined with grammar-constrained decoding (llama.cpp
    json_schema), this gives weak models the structural anchor they
    need to emit valid output even on long/ambiguous turns.
    """
    return {
        "title": "BriefOutput",
        "type": "object",
        "properties": {
            "description": {"type": "string", "minLength": 1},
            "produced_tags": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": _TAG_HARD_CEILING,
            },
            "consumed_tags": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": _TAG_HARD_CEILING,
            },
        },
        "required": ["description", "produced_tags", "consumed_tags"],
        "additionalProperties": False,
    }


_FENCE_OPEN_RE = re.compile(r"^```(?:json|JSON)?\s*\n?")
_FENCE_CLOSE_RE = re.compile(r"\n?```\s*$")
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_brief_output(raw: str) -> BriefOutput:
    """Parse brief LLM output into a normalized ``BriefOutput``.

    Tries strict JSON first; falls back to first balanced object;
    last resort uses the full text as ``description`` with empty
    tags so the brief never loses content even when the LLM strays
    from the schema. Callers don't need to handle exceptions —
    a non-empty BriefOutput always returns.
    """
    text = (raw or "").strip()
    if not text:
        return BriefOutput(description="(empty)", produced_tags=[], consumed_tags=[])

    text_no_fence = _FENCE_CLOSE_RE.sub("", _FENCE_OPEN_RE.sub("", text))

    for candidate in _candidates(text_no_fence):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        try:
            return BriefOutput.model_validate(data)
        except ValidationError:
            # Description missing / wrong type — try next candidate.
            continue

    return BriefOutput(
        description=text_no_fence,
        produced_tags=[],
        consumed_tags=[],
    )


def _candidates(text: str) -> list[str]:
    """Generate candidate JSON strings: the whole text, then the first
    ``{...}`` substring (some models prepend chitchat before JSON)."""
    out = [text]
    m = _OBJECT_RE.search(text)
    if m and m.group(0) != text:
        out.append(m.group(0))
    return out
