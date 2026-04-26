"""In-memory WorkFlow engine — M3 scope (llm_call only).

Design notes:
- Operates on ``schemas.WorkFlow`` Pydantic objects.
- Execution order is Kahn's topological sort (deterministic tie-break).
- Per-node status transitions: planned → running → succeeded | failed.
- ``succeeded`` and ``failed`` are frozen from this point (§4.1, ADR-003).
- Tool calls and sub-agent delegation are explicitly NOT supported here;
  they land in M6. An encountered ``tool_call`` node is marked FAILED
  with a descriptive error so the engine stays honest about its scope.
- The engine takes a *provider callable* as a constructor arg rather
  than owning the adapter directly. Tests inject a stub; production
  wires in an OpenAI-compat adapter.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

from agentloom.engine.events import EventBus, WorkflowEvent
from agentloom.engine.judge_formatter import (
    format_ground_ratio_halt_prompt,
    format_judge_post_prompt,
    format_revise_budget_halt_prompt,
    judge_post_needs_user_input,
)
from agentloom.engine.judge_parser import (
    JudgeParseError,
    judge_verdict_json_schema,
    judge_verdict_tool_def,
    parse_judge_from_tool_args,
    parse_judge_verdict,
)
from agentloom.engine.model_resolution import effective_model_for
from agentloom.engine.recursive_planner_parser import (
    RecursivePlannerOutput,
    planner_grammar_schema,
)
from agentloom.providers.types import (
    AssistantMessage,
    ChatResponse,
    Message,
    SystemMessage,
    ToolDefinition,
    ToolMessage,
    UserMessage,
)
from agentloom.providers.types import ToolUse as ProviderToolUse
from agentloom.schemas import WorkFlow, WorkFlowNode
from agentloom.schemas.chatflow import CompactPreserveMode
from agentloom.schemas.common import (
    EditableText,
    EditProvenance,
    JudgeVariant,
    NodeScope,
    NodeStatus,
    ProviderModelRef,
    StepKind,
    TokenUsage,
    WorkNodeRole,
    utcnow,
)
from agentloom.schemas.common import ToolUse as SchemaToolUse
from agentloom.schemas.workflow import CompactSnapshot, WireMessage
from agentloom.tools.base import SideEffect, ToolContext, ToolRegistry

#: Provider call surface — the engine never instantiates an adapter
#: directly, the caller injects a closure. ``on_token`` is the
#: streaming hook: when supplied, the closure should run the provider
#: with stream=true and forward each fragment via the callback so the
#: engine can republish a live preview to the bus. ``None`` keeps the
#: legacy non-streaming behavior so test doubles don't have to
#: implement streaming.
ProviderCall = Callable[
    ...,
    Awaitable[ChatResponse],
]
TokenCallback = Callable[[str], Awaitable[None]]

#: Callback fired after a node transitions to SUCCEEDED. The hook is
#: free to mutate ``workflow`` (typically: add new nodes that the next
#: ``execute()`` iteration will pick up). Used to keep the inner DAG
#: dynamic — e.g. judge_pre's verdict decides whether the WorkFlow
#: continues with an llm_call or routes straight to judge_post.
PostNodeHook = Callable[[WorkFlow, WorkFlowNode], None]

#: Resolve a model's context window in tokens. ChatFlowEngine wires
#: this to a closure that reads ``ModelInfo.context_window`` from the
#: provider registry. ``None`` on either the callable or its result
#: means "unknown"; the engine falls back to
#: :data:`DEFAULT_CONTEXT_WINDOW_TOKENS`.
ContextWindowLookup = Callable[[ProviderModelRef | None], int | None]

#: MemoryBoard persistence sink. ``_run_brief`` calls this with the
#: distilled description, scope, and source node metadata; the closure
#: is responsible for upserting a ``BoardItem`` row (idempotent by
#: ``source_node_id``). ``None`` means "no board configured" — the
#: engine then skips both brief auto-spawn and the board write,
#: which keeps bare-engine tests that don't exercise MemoryBoard
#: running against their existing fixtures.
BoardWriter = Callable[..., Awaitable[None]]

#: MemoryBoard read path. Consumers downstream of a brief (judge_post's
#: layer-notes) pull the distilled
#: ``description`` rows from the board rather than walking the DAG's
#: brief WorkNodes, so briefs keep a single-edge topology (parent →
#: brief only) and never show up as synthetic parents of other nodes.
#: The closure takes ``workflow_id`` and returns the list of
#: ``dict``-shaped rows (at minimum: ``source_kind``, ``scope``,
#: ``description``). ``None`` means "no board configured" — the engine
#: then renders an empty layer-notes block, matching the pre-PR-A
#: fallback on workflows that ran without a writer.
BoardReader = Callable[..., Awaitable[list[dict[str, Any]]]]

#: Sentinel so we can distinguish ``chatflow_tool_loop_budget=None``
#: ("chatflow exists and says unlimited") from "no chatflow was passed
#: at all".
_UNSET: object = object()


#: Fallback tool-loop budget used when no ChatFlow/WorkFlow context is
#: provided (e.g. engine tests that exercise a bare WorkFlow). Real
#: traffic resolves the budget via
#: ``workflow.tool_loop_budget ?? chatflow.tool_loop_budget`` — see
#: ``_effective_tool_loop_budget`` below. ``None`` on either layer
#: means "unlimited"; this default exists only so standalone callers
#: aren't implicitly unlimited.
MAX_TOOL_LOOP_ITERATIONS = 12


# --------------------------------------------------------------- compact (Tier 1)

#: Default per-ChatFlow compact trigger threshold. Pre-llm_call the
#: engine estimates the message-list token footprint (char-based); if
#: ``estimate / context_window >= TRIGGER_PCT`` a compact WorkNode is
#: inserted before the call runs. Chatflow settings will override this
#: per-flow in step 5.
DEFAULT_COMPACT_TRIGGER_PCT = 0.7
#: Default target footprint for the compact summary. Fed to the compact
#: worker as ``target_tokens = context_window * TARGET_PCT``.
DEFAULT_COMPACT_TARGET_PCT = 0.5
#: Default number of trailing messages kept verbatim on the downstream
#: side of a compact. Smaller = more aggressive compaction, larger =
#: more fidelity. Chatflow settings will override per-flow in step 5.
DEFAULT_COMPACT_KEEP_RECENT_COUNT = 3
#: Default strategy for deciding the verbatim tail. ``by_count`` matches
#: the original engine behavior (N-based preserve, summary capped).
DEFAULT_COMPACT_PRESERVE_MODE: CompactPreserveMode = "by_count"
#: Fallback context window (in tokens) used when the model's actual
#: ``context_window`` is unknown. Matches the frontend's
#: ``DEFAULT_MAX_CONTEXT_TOKENS`` so UI bar and engine threshold agree.
DEFAULT_CONTEXT_WINDOW_TOKENS = 32_000


class _CompactRequested(Exception):
    """Raised inside ``_invoke_and_freeze`` when the pending message
    list exceeds the compact trigger. Caught by ``_run_node`` which
    un-winds the current node back to ``planned`` so the execute loop
    picks up the freshly-inserted compact WorkNode on its next pass.
    """


@functools.lru_cache(maxsize=1)
def _get_token_encoder() -> Any:
    """Return a cached tiktoken encoder, or ``None`` if tiktoken can't
    load (e.g. offline and encoding file not cached). ``o200k_base`` is
    the GPT-4o tokenizer — a reasonable cross-provider approximation
    with much better Chinese/CJK coverage than the old ``chars // 4``
    heuristic (≈0.67 tokens/汉字, ≈0.24 tokens/ASCII-char).

    We deliberately pick one encoding rather than per-provider ones:
    Anthropic's tokenizer isn't publicly available, Ark / Volcengine /
    Ollama use proprietary or model-specific BPEs, and threading a
    model reference into every estimator call site is churn without
    meaningful precision gain for the things these numbers drive
    (compact trigger thresholds and a display bar).
    """
    try:
        import tiktoken
        return tiktoken.get_encoding("o200k_base")
    except Exception:  # noqa: BLE001 — defensive: never block on estimator
        return None


def _count_text_tokens(text: str) -> int:
    """Token count for a single string. Uses tiktoken when available,
    falls back to ``chars // 4`` if the encoder failed to load."""
    if not text:
        return 0
    enc = _get_token_encoder()
    if enc is None:
        return len(text) // 4
    return len(enc.encode(text, disallowed_special=()))


def _estimate_tokens_from_provider_messages(messages: list[Message]) -> int:
    """Token estimate for a provider-side message list using tiktoken
    ``o200k_base``. Matches the shape of the inputs ``_invoke_and_freeze``
    has already built, so the trigger fires on exactly what would hit
    the wire.
    """
    total = 0
    for m in messages:
        content = getattr(m, "content", None) or ""
        total += _count_text_tokens(content)
        tool_uses = getattr(m, "tool_uses", None)
        if tool_uses:
            for tu in tool_uses:
                total += _count_text_tokens(tu.name)
                import json as _json
                total += _count_text_tokens(
                    _json.dumps(tu.arguments, default=str)
                )
    return total


def _estimate_tokens_from_wire(messages: list[WireMessage]) -> int:
    """Sibling of :func:`_estimate_tokens_from_provider_messages` for
    schema-side ``WireMessage`` lists. Used by snapshot accounting."""
    total = 0
    for w in messages:
        total += _count_text_tokens(w.content or "")
        for tu in w.tool_uses:
            total += _count_text_tokens(tu.name)
            import json as _json
            total += _count_text_tokens(
                _json.dumps(tu.arguments, default=str)
            )
    return total


def _truncate_text_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate *text* so its estimated token count is ≤ ``max_tokens``.

    Used as a last-resort hard cap when the compact worker returns a
    summary longer than the caller's ``target_tokens`` budget. Without
    this cap, a chatty summarizer can produce a post-compact context
    that still overshoots the model's window — breaking the
    ``trigger_pct + target_pct ≤ 1.0`` invariant the engine relies on.

    When tiktoken is available the slice is exact; otherwise we fall
    back to the ``chars // 4`` heuristic that matches
    :func:`_count_text_tokens`.
    """
    if max_tokens <= 0 or not text:
        return ""
    enc = _get_token_encoder()
    if enc is None:
        cap = max_tokens * 4
        if len(text) <= cap:
            return text
        return text[:cap]
    ids = enc.encode(text, disallowed_special=())
    if len(ids) <= max_tokens:
        return text
    return enc.decode(ids[:max_tokens])


def _greedy_pack_tail_within_budget(
    messages: list[Message], budget_tokens: int
) -> list[Message]:
    """Pick the longest suffix of *messages* that fits in *budget_tokens*.

    Walks the list from the newest side back: admits each message if
    adding it still fits; stops at the first overflow (cannot skip a
    middle message — that would leave a conversational hole). Returns
    the kept messages in original (chronological) order.
    """
    if budget_tokens <= 0 or not messages:
        return []
    remaining = budget_tokens
    packed_reversed: list[Message] = []
    for msg in reversed(messages):
        msg_tokens = _estimate_tokens_from_provider_messages([msg])
        if msg_tokens > remaining:
            break
        packed_reversed.append(msg)
        remaining -= msg_tokens
    return list(reversed(packed_reversed))


_COMPACT_FIXTURE_CACHE: dict[str, tuple[dict[str, Any], dict[str, str]]] = {}


def _get_compact_fixture() -> tuple[dict[str, Any], dict[str, str]]:
    """Return ``(plan_dict, include_fragments)`` for ``compact.yaml`` in
    the workspace's currently-configured language.

    Loaded once per language at first use and cached. Fails loudly if
    the fixture is missing because Tier 1 can't function without it —
    the compact worker has no fallback prompt.
    """
    from agentloom import tenancy_runtime
    from agentloom.db.models.tenancy import DEFAULT_WORKSPACE_ID
    from agentloom.templates.loader import (
        DEFAULT_LANGUAGE,
        fragments_as_texts,
        load_fixtures,
    )

    lang = tenancy_runtime.get_settings(DEFAULT_WORKSPACE_ID).language
    cached = _COMPACT_FIXTURE_CACHE.get(lang)
    if cached is not None:
        return cached
    templates, fragments = load_fixtures(language=lang)
    compact = next((f for f in templates if f.builtin_id == "compact"), None)
    if compact is None and lang != DEFAULT_LANGUAGE:
        templates, fragments = load_fixtures(language=DEFAULT_LANGUAGE)
        compact = next((f for f in templates if f.builtin_id == "compact"), None)
    if compact is None:
        raise RuntimeError(
            "compact.yaml fixture missing — required for Tier 1 auto-compact"
        )
    _COMPACT_FIXTURE_CACHE[lang] = (compact.plan, fragments_as_texts(fragments))
    return _COMPACT_FIXTURE_CACHE[lang]


def _compact_description_text() -> EditableText:
    """Placeholder description for engine-inserted compact nodes. Kept
    in one spot so the UI and tests share the same string."""
    return EditableText(
        text="Compact (auto-inserted)",
        provenance=EditProvenance.PURE_AGENT,
    )


# --------------------------------------------------------------- brief (MemoryBoard PR 1)

#: Max characters of a tool_call's ``tool_result.content`` we splice
#: into the deterministic brief template. Briefs are supposed to be
#: compact; carrying a 40 KB shell dump would defeat the purpose.
_BRIEF_TOOL_RESULT_SNIPPET_CHARS = 240

_BRIEF_FIXTURE_CACHE: dict[tuple[str, str], tuple[dict[str, Any], dict[str, str]]] = {}


def _get_brief_fixture(
    builtin_id: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Return ``(plan_dict, include_fragments)`` for the MemoryBoard
    brief fixture identified by *builtin_id* (``"node_brief"`` —
    ``"flow_brief"`` was retired 2026-04-21) in the workspace's
    currently-configured language.

    Mirrors :func:`_get_compact_fixture` — cached per (language,
    builtin_id). Fails loudly if the fixture is missing because the
    brief LLM path can't function without it; the caller's code-template
    fallback only activates on *runtime* failures (LLM exception), not
    on missing plans.
    """
    from agentloom import tenancy_runtime
    from agentloom.db.models.tenancy import DEFAULT_WORKSPACE_ID
    from agentloom.templates.loader import (
        DEFAULT_LANGUAGE,
        fragments_as_texts,
        load_fixtures,
    )

    lang = tenancy_runtime.get_settings(DEFAULT_WORKSPACE_ID).language
    key = (lang, builtin_id)
    cached = _BRIEF_FIXTURE_CACHE.get(key)
    if cached is not None:
        return cached
    templates, fragments = load_fixtures(language=lang)
    fx = next((f for f in templates if f.builtin_id == builtin_id), None)
    if fx is None and lang != DEFAULT_LANGUAGE:
        templates, fragments = load_fixtures(language=DEFAULT_LANGUAGE)
        fx = next((f for f in templates if f.builtin_id == builtin_id), None)
    if fx is None:
        raise RuntimeError(
            f"{builtin_id}.yaml fixture missing — required for "
            f"MemoryBoard brief auto-spawn"
        )
    _BRIEF_FIXTURE_CACHE[key] = (fx.plan, fragments_as_texts(fragments))
    return _BRIEF_FIXTURE_CACHE[key]


def _brief_description_text(scope: NodeScope) -> EditableText:
    """Placeholder description for engine-inserted brief nodes.
    Mirrors :func:`_compact_description_text` — centralized so UI and
    tests agree on the string the user sees next to an auto-brief.
    Only ``NodeScope.NODE`` is ever spawned after 2026-04-21; the
    ``FLOW`` branch remains only as a label for any legacy rows
    still persisted under ``scope='flow'``."""
    label = "Node brief" if scope == NodeScope.NODE else "Flow brief"
    return EditableText(
        text=f"{label} (auto-inserted)",
        provenance=EditProvenance.PURE_AGENT,
    )


def _brief_code_template_for_node(source: WorkFlowNode) -> str:
    """Return the deterministic fallback description for *source*.

    Used on the two code paths that skip the LLM: (1) tool_call source
    — running a model call over every shell-output would blow up ReAct
    loop cost — and (2) LLM brief failure fallback. Keep the format
    stable and terse: ``<kind>: <hint>``, optionally flagged ``[error]``.
    """
    kind = source.step_kind.value
    if source.step_kind == StepKind.TOOL_CALL:
        tool_name = source.tool_name or "?"
        tr = source.tool_result
        if tr is None:
            return f"tool_call {tool_name}: no result captured"
        first_line = (tr.content or "").splitlines()[0] if tr.content else ""
        snippet = first_line[:_BRIEF_TOOL_RESULT_SNIPPET_CHARS]
        if len(first_line) > _BRIEF_TOOL_RESULT_SNIPPET_CHARS:
            snippet += "…"
        tag = " [error]" if tr.is_error else ""
        return f"tool_call {tool_name}{tag}: {snippet}".rstrip()
    # Non-tool kinds: try the output message then the description.
    out = source.output_message
    body = (out.content if out is not None else "") or ""
    first_line = body.splitlines()[0] if body else ""
    snippet = first_line[:_BRIEF_TOOL_RESULT_SNIPPET_CHARS]
    if len(first_line) > _BRIEF_TOOL_RESULT_SNIPPET_CHARS:
        snippet += "…"
    if not snippet and source.description is not None:
        snippet = source.description.text[:_BRIEF_TOOL_RESULT_SNIPPET_CHARS]
    return f"{kind}: {snippet}".rstrip(": ")


def _parent_is_fresh_compact(workflow: WorkFlow, node: WorkFlowNode) -> bool:
    """True iff ``node`` has a direct parent that is a COMPACT WorkNode
    with a settled snapshot. Used to break the Tier 1 loop: after a
    compact finishes, the node it was inserted for re-runs exactly
    once with the summarized context — it must not trigger another
    compact even if the summary + preserved tail still overflow.
    """
    for pid in node.parent_ids:
        parent = workflow.get(pid)
        if (
            parent.step_kind == StepKind.COMPRESS
            and parent.compact_snapshot is not None
            and parent.compact_snapshot.summary
        ):
            return True
    return False


def _render_messages_to_compact(
    tagged: list[tuple[str | None, Message]],
) -> str:
    """Serialize a provider-message list into the
    ``[node:<id> | role] body`` shape the compact worker's prompt
    expects. The ``<id>`` prefix lets the worker cite source WorkNodes
    next to each summary segment so readers can drill back into the
    original content via ``get_node_context``. ``None`` ids — synthetic
    entries like prior-compact preambles — render as ``[node:?]``.
    """
    lines: list[str] = []
    for node_id, m in tagged:
        tag = node_id or "?"
        if isinstance(m, SystemMessage):
            lines.append(f"[node:{tag} | system] {m.content}")
        elif isinstance(m, UserMessage):
            lines.append(f"[node:{tag} | user] {m.content}")
        elif isinstance(m, AssistantMessage):
            body = m.content or ""
            if m.tool_uses:
                import json as _json
                parts = [
                    f"{tu.name}({_json.dumps(tu.arguments, default=str)})"
                    for tu in m.tool_uses
                ]
                body = (body + "\n" if body else "") + "tool_uses: " + "; ".join(parts)
            lines.append(f"[node:{tag} | assistant] {body}")
        elif isinstance(m, ToolMessage):
            lines.append(
                f"[node:{tag} | tool:{m.tool_use_id}] {m.content}"
            )
    return "\n".join(lines)


#: Cap the per-message fallback when a brief isn't available — prevents
#: a single outsized tool_result dump from defeating the whole point of
#: the brief-based compaction. 400 chars ≈ 100 tokens, fits the "one
#: line description" target used elsewhere in the codebase.
_BRIEF_FALLBACK_MAX_CHARS = 400


#: Tier 0 tool-result cap (Claude Code style). When a single
#: ``tool_result.content`` exceeds ``_TOOL_RESULT_INLINE_CAP`` characters
#: at context-assembly time, the LLM sees the first
#: ``_TOOL_RESULT_PREVIEW_HEAD`` characters plus a footer pointing to
#: the source WorkNode id. Full content stays intact on
#: ``WorkNode.tool_result`` — downstream workers that actually need
#: detail call ``get_node_context(node_id=...)`` to retrieve it. This
#: stops a single 500 KB ``Read`` or ``tavily_research`` response from
#: blowing the next llm_call's prompt budget before any compaction
#: tier can kick in (tiers 1 / 2 only cover *ancestor* history, not a
#: single oversized message written in the current turn).
_TOOL_RESULT_INLINE_CAP = 30_000
_TOOL_RESULT_PREVIEW_HEAD = 2_000


def _maybe_truncate_tool_result(
    content: str,
    wn_id: str,
    tool_name: str | None,
) -> str:
    """Head-truncate ``content`` if it exceeds the Tier 0 inline cap,
    else return it verbatim. The truncated form is LLM-facing only —
    the caller must preserve the original string on ``WorkNode.
    tool_result`` so ``get_node_context`` can still return the full
    body. See ``_TOOL_RESULT_INLINE_CAP`` for the trigger and the
    rationale.

    Emits an info-level log line on every trigger so operators can see
    hit rate (grep ``tool_result_cap_triggered`` in ``backend.log``).
    Used to decide whether the 30 KB default cap is too tight / loose
    once real traffic lands."""
    if len(content) <= _TOOL_RESULT_INLINE_CAP:
        return content
    head = content[:_TOOL_RESULT_PREVIEW_HEAD]
    tool_label = tool_name or "?"
    remaining = len(content) - _TOOL_RESULT_PREVIEW_HEAD
    reduction_pct = (
        100.0 * (len(content) - _TOOL_RESULT_PREVIEW_HEAD) / len(content)
    )
    logging.getLogger(__name__).info(
        "tool_result_cap_triggered: tool=%s wn_id=%s original=%d "
        "preview=%d reduction=%.1f%%",
        tool_label,
        wn_id,
        len(content),
        _TOOL_RESULT_PREVIEW_HEAD,
        reduction_pct,
    )
    return (
        f"[tool_result preview — {tool_label} returned {len(content):,} "
        f"characters; call get_node_context(node_id={wn_id!r}) for the "
        f"full content.]\n\n"
        f"{head}\n\n"
        f"[… {remaining:,} more characters truncated. Full tool_result "
        f"persists on WorkNode {wn_id}; use get_node_context to read it.]"
    )


def _rewrite_tagged_as_briefs(
    tagged: list[tuple[str | None, Message]],
    briefs_by_node: dict[str, str],
) -> list[tuple[str | None, Message]]:
    """Return a copy of *tagged* where each entry's ``content`` is
    replaced by ``[brief + node_id]`` so the compact prompt stays small
    regardless of how bloated the original tool_results were.

    - A MemoryBoard brief takes precedence (one-line prose already
      distilled by the node-brief LLM).
    - If no brief exists for a given node_id (synthetic messages,
      off-axis nodes), fall back to the first 400 chars of the original
      content — lossy but bounded.
    - ``node_id is None`` (synthetic preamble, e.g. prior-compact
      preserved tail) is kept verbatim since it already carries a
      summary.

    Role, tool_use metadata, and tool_use_id threading are preserved by
    constructing the concrete Message subclass in place — tool calls
    can't be hollowed out without breaking OpenAI-style tool-result
    linkage.
    """
    out: list[tuple[str | None, Message]] = []
    for node_id, msg in tagged:
        if node_id is None:
            out.append((node_id, msg))
            continue
        brief = briefs_by_node.get(node_id)
        if brief:
            new_content = f"[node:{node_id}] {brief.strip()}"
        else:
            raw = msg.content or ""
            if len(raw) > _BRIEF_FALLBACK_MAX_CHARS:
                raw = raw[:_BRIEF_FALLBACK_MAX_CHARS].rstrip() + "…"
            new_content = f"[node:{node_id}] {raw}"
        if isinstance(msg, SystemMessage):
            out.append((node_id, SystemMessage(content=new_content)))
        elif isinstance(msg, UserMessage):
            out.append((node_id, UserMessage(content=new_content)))
        elif isinstance(msg, AssistantMessage):
            # Preserve tool_uses so downstream pairing stays valid; the
            # callee only reads ``content`` for the compact prompt text.
            out.append(
                (
                    node_id,
                    AssistantMessage(
                        content=new_content, tool_uses=msg.tool_uses
                    ),
                )
            )
        elif isinstance(msg, ToolMessage):
            out.append(
                (
                    node_id,
                    ToolMessage(
                        content=new_content, tool_use_id=msg.tool_use_id
                    ),
                )
            )
        else:  # pragma: no cover — exhaustive over Message subtypes
            out.append((node_id, msg))
    return out


def _any_resolved_model(workflow: WorkFlow) -> ProviderModelRef | None:
    """Pick any main-axis node's ``resolved_model`` as a fallback brief
    model. Used by the DELEGATE-brief path: a DELEGATE WorkNode never
    invokes an LLM on its own axis, so ``resolved_model`` is always
    None on it — borrow a sibling's model so the brief runs via LLM
    instead of the code template. ``None`` if the WorkFlow is empty
    or no non-BRIEF node carries a ``resolved_model`` (pure-stub
    tests)."""
    for node in workflow.nodes.values():
        if node.step_kind == StepKind.BRIEF:
            continue
        if node.resolved_model is not None:
            return node.resolved_model
        if node.model_override is not None:
            return node.model_override
    return None


def _render_source_inputs_for_brief(source: WorkFlowNode) -> str:
    """Render *source*'s ``input_messages`` into the prose block a
    node-brief template expects.

    ``None`` input_messages (ancestor-built context) renders as an
    explicit marker so the summarizer knows not to invent inputs.
    """
    if not source.input_messages:
        return "(no explicit input_messages on this node)"
    lines: list[str] = []
    for msg in source.input_messages:
        body = msg.content or ""
        if msg.tool_uses:
            import json as _json

            parts = [
                f"{tu.name}({_json.dumps(tu.arguments, default=str)})"
                for tu in msg.tool_uses
            ]
            body = (body + "\n" if body else "") + "tool_uses: " + "; ".join(parts)
        lines.append(f"[{msg.role}] {body}")
    return "\n".join(lines)


def _render_source_output_for_brief(source: WorkFlowNode) -> str:
    """Render *source*'s terminal output for a node-brief prompt.

    Each StepKind stores its output in a different field — centralise
    the dispatch so the template always receives a single opaque
    string regardless of the source kind.
    """
    if source.step_kind == StepKind.TOOL_CALL:
        # tool_call briefs bypass the LLM entirely, so this branch is
        # only ever hit via the fallback path.
        tr = source.tool_result
        if tr is None:
            return "(no tool_result captured)"
        err = " [error]" if tr.is_error else ""
        return f"{tr.content or ''}{err}".strip() or "(empty tool result)"
    if source.step_kind == StepKind.COMPRESS and source.compact_snapshot is not None:
        return source.compact_snapshot.summary or "(empty compact snapshot)"
    if source.step_kind == StepKind.JUDGE_CALL and source.judge_verdict is not None:
        # The raw verdict is structured JSON; the textual ``output_message``
        # already contains what the model wrote, but the structured
        # verdict itself is what downstream consumers read. Hand both.
        out = (source.output_message.content or "") if source.output_message else ""
        return out or "(no textual judge output — see verdict fields)"
    if source.step_kind == StepKind.DELEGATE and source.sub_workflow is not None:
        # DELEGATE carries no output_message of its own — its "result"
        # is whatever the sub-WorkFlow produced. Under auto_plan the
        # chronological tail is usually a judge_post whose ``content``
        # is empty (the verdict is structured in separate fields), so
        # a naive "latest non-brief with output_message" picker returns
        # an empty string and the node-brief LLM concludes the delegate
        # "produced no output". 2026-04-22 integration test found this:
        # three succeeded delegates all got briefs that read "该委托节点
        # 未产生任何输出内容". Prefer in priority order:
        #   1. judge_post.accept.merged_response (layer-aggregated reply)
        #   2. judge_post.accept → latest worker draft (atomic happy path)
        #   3. Latest worker DRAFT output (no judge_post)
        #   4. Latest non-BRIEF / non-JUDGE_CALL output (any content)
        #   5. The empty-output sentinel
        sub = source.sub_workflow
        post_judges = [
            sn
            for sn in sub.nodes.values()
            if sn.step_kind == StepKind.JUDGE_CALL
            and sn.judge_variant == JudgeVariant.POST
            and sn.status == NodeStatus.SUCCEEDED
            and sn.judge_verdict is not None
        ]
        # Latest judge_post first (aggregator wins over earlier atomic).
        post_judges.sort(
            key=lambda n: n.finished_at or n.updated_at or n.created_at,
            reverse=True,
        )
        for pj in post_judges:
            v = pj.judge_verdict
            if v is not None and v.post_verdict == "accept" and v.merged_response:
                return v.merged_response

        latest_worker: WorkFlowNode | None = None
        latest_any: WorkFlowNode | None = None
        for sn in sub.nodes.values():
            if sn.step_kind == StepKind.BRIEF:
                continue
            if sn.status != NodeStatus.SUCCEEDED:
                continue
            if sn.output_message is None:
                continue
            content = (sn.output_message.content or "").strip()
            if not content:
                # Empty content (e.g. judge_call whose verdict lives in
                # structured fields, or an llm_call that only produced
                # tool_uses) never makes a usable brief input — skip.
                continue
            if sn.step_kind == StepKind.JUDGE_CALL:
                # A judge_call's textual content is its free-form prose;
                # prefer real workers over a judge's preamble.
                continue
            if (
                sn.step_kind == StepKind.DRAFT
                and sn.role == WorkNodeRole.WORKER
            ):
                if latest_worker is None or (sn.finished_at or utcnow()) > (
                    latest_worker.finished_at or utcnow()
                ):
                    latest_worker = sn
            else:
                if latest_any is None or (sn.finished_at or utcnow()) > (
                    latest_any.finished_at or utcnow()
                ):
                    latest_any = sn
        if latest_worker is not None and latest_worker.output_message is not None:
            return latest_worker.output_message.content or ""
        if latest_any is not None and latest_any.output_message is not None:
            return latest_any.output_message.content or ""
        return "(sub-WorkFlow produced no textual output)"
    if source.output_message is not None:
        return source.output_message.content or ""
    return "(no output produced)"


#: Sentinel embedded in the runtime-injected layer_notes system message
#: so a judge clone (spawned by ``_after_judge_failed`` after a parse
#: crash) can detect that its copied input_messages already carry the
#: block and skip re-appending it. The marker isn't user-facing — it
#: sits on its own line at the top of the appended content.
_LAYER_NOTES_MARKER = "[__layer_notes__]"

#: Max follow-up attempts when the judge's first response fails JSON
#: parse. 2 retries means 3 total provider calls on the worst case.
#: Observed failure mode (2026-04-22 regression): ark-code-latest
#: returns empty content on the first try, reproduces on a single
#: retry, then succeeds on the second — a one-retry budget cost the
#: whole sub-WorkFlow. See ``_run_judge_call``'s parse branch.
_JUDGE_PARSE_MAX_RETRIES = 2

#: Base delay (seconds) before the first judge-parse retry. Doubles
#: each subsequent attempt (0.5s → 1.0s). Gives transient provider
#: errors — 429 / thinking-mode truncation / rate-limited content
#: filters — a moment to clear without spamming the endpoint.
_JUDGE_PARSE_RETRY_BASE_DELAY = 0.5

#: M7.5 cognitive roles: judges + planner. These nodes do not take
#: tool actions on the world — they reason about what the worker
#: should do — so the registry filter restricts them to NONE / READ
#: side-effect tools. Even if a future change flips ``expose_tools``
#: on for one of these roles by accident, ``resolve_for_node`` will
#: still drop any WRITE tool from their visible set.
_COGNITIVE_ROLES: frozenset[WorkNodeRole] = frozenset(
    {
        WorkNodeRole.PRE_JUDGE,
        WorkNodeRole.PLAN,
        WorkNodeRole.PLAN_JUDGE,
        WorkNodeRole.WORKER_JUDGE,
        WorkNodeRole.POST_JUDGE,
    }
)

#: Side-effect set permitted to cognitive roles. ``NONE`` covers the
#: rare pure-compute tool; ``READ`` covers filesystem reads, registry
#: lookups, get_node_context, MemoryBoard lookups. Excludes ``WRITE``.
_COGNITIVE_SIDE_EFFECTS: frozenset[SideEffect] = frozenset(
    {SideEffect.NONE, SideEffect.READ}
)


def _side_effect_filter_for(node: WorkFlowNode) -> set[SideEffect] | None:
    """Return the side-effect filter that should apply to *node*'s tool
    visibility, or ``None`` to skip the filter.

    Cognitive roles (judges + planner) get ``{NONE, READ}``. Every
    other role — including ``WORKER`` (the executing draft) and the
    ``DELEGATE`` re-distribution path, plus legacy nodes whose
    ``role`` is ``None`` — falls through with no extra filter so they
    can call WRITE tools.
    """
    if node.role in _COGNITIVE_ROLES:
        return set(_COGNITIVE_SIDE_EFFECTS)
    return None


class WorkflowEngine:
    def __init__(
        self,
        provider_call: ProviderCall,
        event_bus: EventBus,
        tool_registry: ToolRegistry | None = None,
        tool_context: ToolContext | None = None,
        *,
        context_window_lookup: ContextWindowLookup | None = None,
        compact_trigger_pct: float = DEFAULT_COMPACT_TRIGGER_PCT,
        compact_target_pct: float = DEFAULT_COMPACT_TARGET_PCT,
        compact_keep_recent_count: int = DEFAULT_COMPACT_KEEP_RECENT_COUNT,
        compact_preserve_mode: CompactPreserveMode = DEFAULT_COMPACT_PRESERVE_MODE,
        compact_model: ProviderModelRef | None = None,
        board_writer: BoardWriter | None = None,
        board_reader: BoardReader | None = None,
    ) -> None:
        self._provider_call = provider_call
        self._bus = event_bus
        self._tools = tool_registry
        self._tool_ctx = tool_context or ToolContext()
        #: Resolves a model's context_window in tokens. ``None`` means
        #: "no lookup plumbed in" — the engine treats every model as
        #: having :data:`DEFAULT_CONTEXT_WINDOW_TOKENS`.
        self._context_window_lookup = context_window_lookup
        #: MemoryBoard persistence sink. ``None`` switches the brief
        #: auto-spawn off wholesale so bare-engine tests don't grow
        #: phantom brief children they never asked for.
        self._board_writer = board_writer
        #: MemoryBoard read path. Used by judge_post's layer-notes so
        #: downstream consumers read brief text from the board instead
        #: of treating brief WorkNodes as synthetic parents (PR A,
        #: 2026-04-21). flow_brief was retired in the same pass.
        self._board_reader = board_reader
        #: Populated per ``execute()`` from the ``chatflow_id`` kwarg.
        #: The brief writer stamps it onto every BoardItem row.
        self._chatflow_id: str | None = None
        #: Compact Tier 1 parameters. Chatflow-level overrides flow in
        #: via the ChatFlowEngine wiring at construction time.
        self._compact_trigger_pct = compact_trigger_pct
        self._compact_target_pct = compact_target_pct
        self._compact_keep_recent_count = compact_keep_recent_count
        self._compact_preserve_mode: CompactPreserveMode = compact_preserve_mode
        self._compact_model = compact_model
        #: Per-``execute()`` filter — tool names hidden from the LLM and
        #: refused if invoked. Populated from the chatflow's
        #: ``disabled_tool_names`` list by ChatFlowEngine. Empty
        #: frozenset means "no extra filter on top of constraints".
        self._disabled_tool_names: frozenset[str] = frozenset()
        #: Resolved once per ``execute()`` call; read by
        #: :func:`_assert_tool_loop_budget`. ``None`` means unlimited.
        self._effective_budget: int | None = MAX_TOOL_LOOP_ITERATIONS
        #: Resolved once per ``execute()`` call — the cap on
        #: ``judge_during.during_verdict == "revise"`` seen in this run
        #: before auto-mode halts. ``None`` means unlimited (§5.3 FR-PL-7).
        self._effective_revise_budget: int | None = None
        #: Revise counter for *this* ``execute()`` invocation. Nested
        #: sub_agent_delegation will spin up its own engine, so each
        #: recursion level counts independently.
        self._revise_count: int = 0
        #: Per-``execute()`` hook fired on every node success. Lets the
        #: caller (typically ChatFlowEngine) grow the DAG dynamically —
        #: e.g. spawn judge_post once judge_pre/llm_call has settled.
        self._post_node_hook: PostNodeHook | None = None
        #: Resolved once per ``execute()``. ``None`` means the
        #: planner-grounding fuse is disabled for this run.
        self._effective_min_ground_ratio: float | None = None
        #: Minimum completed leaves before the grounding fuse arms.
        self._effective_ground_ratio_grace: int = 20
        #: Resolved at execute() start from chatflow-level overrides,
        #: falling back to the constructor defaults. Read by
        #: :meth:`_needs_compact` / :meth:`_insert_compact_worknode` so
        #: different ChatFlows can run different compact policies
        #: against a single shared engine instance.
        self._effective_compact_trigger_pct: float | None = compact_trigger_pct
        self._effective_compact_target_pct: float = compact_target_pct
        self._effective_compact_keep_recent_count: int = compact_keep_recent_count
        self._effective_compact_preserve_mode: CompactPreserveMode = compact_preserve_mode
        self._effective_compact_model: ProviderModelRef | None = compact_model
        #: Pre-resolved system-prompt prefix injected before every
        #: tool-bearing LLM call — ChatFlowEngine builds it (static user
        #: text + dynamic OS / shell / Bash-disabled hint) and hands it
        #: in via ``execute(chatflow_runtime_environment_note=...)``.
        #: Empty string disables the prepend; default unset (= empty)
        #: keeps bare-engine tests that don't pipe a chatflow through
        #: behaving exactly as before.
        self._effective_runtime_note: str = ""
        #: ChatFlow-level caps on brief tag emissions, threaded into
        #: ``_spawn_node_brief`` so the fixture's prompt bounds the
        #: emitted ``produced_tags`` / ``consumed_tags`` arrays. Defaults
        #: mirror the schema (10 / 8) so a bare engine without chatflow
        #: context still produces a sensible brief.
        self._effective_max_produced_tags: int = 10
        self._effective_max_consumed_tags: int = 8

    async def execute(
        self,
        workflow: WorkFlow,
        *,
        chatflow_tool_loop_budget: int | None | object = _UNSET,
        chatflow_auto_mode_revise_budget: int | None | object = _UNSET,
        chatflow_min_ground_ratio: float | None | object = _UNSET,
        chatflow_ground_ratio_grace_nodes: int | object = _UNSET,
        chatflow_compact_trigger_pct: float | None | object = _UNSET,
        chatflow_compact_target_pct: float | object = _UNSET,
        chatflow_compact_keep_recent_count: int | object = _UNSET,
        chatflow_compact_preserve_mode: CompactPreserveMode | object = _UNSET,
        chatflow_compact_model: ProviderModelRef | None | object = _UNSET,
        chatflow_runtime_environment_note: str | object = _UNSET,
        chatflow_max_produced_tags: int | object = _UNSET,
        chatflow_max_consumed_tags: int | object = _UNSET,
        chatflow_id: str | None = None,
        post_node_hook: PostNodeHook | None = None,
        disabled_tool_names: frozenset[str] | None = None,
    ) -> WorkFlow:
        """Run every planned node in topological order. Mutates and
        returns the workflow.

        Already-frozen nodes are skipped (they belong to a prior
        execution). A node whose ancestors include a failed node is
        also skipped — failure does not cascade execution, but we do
        not run downstream nodes whose context is broken.

        The tool-use loop (M6) can add new nodes to ``workflow``
        mid-execution: when an llm_call emits ``tool_uses`` we
        auto-spawn child tool_call nodes + a follow-up llm_call. We
        handle this by recomputing the order after each step and
        running any newly-planned node we haven't seen yet.

        ``chatflow_tool_loop_budget`` lets the caller (typically
        ``ChatFlowEngine``) hand in the outer ChatFlow's budget so the
        resolution ``workflow.tool_loop_budget ?? chatflow.tool_loop_budget``
        can finish. ``None`` explicitly means "unlimited"; the
        ``_UNSET`` sentinel means "no chatflow context" and falls back
        to :data:`MAX_TOOL_LOOP_ITERATIONS`.
        """
        self._effective_budget = _effective_tool_loop_budget(
            workflow.tool_loop_budget, chatflow_tool_loop_budget
        )
        self._effective_revise_budget = _effective_revise_budget(
            workflow.auto_mode_revise_budget, chatflow_auto_mode_revise_budget
        )
        self._effective_min_ground_ratio = (
            None if chatflow_min_ground_ratio is _UNSET else chatflow_min_ground_ratio  # type: ignore[assignment]
        )
        self._effective_ground_ratio_grace = (
            20 if chatflow_ground_ratio_grace_nodes is _UNSET else chatflow_ground_ratio_grace_nodes  # type: ignore[assignment]
        )
        # Compact overrides: _UNSET falls through to constructor
        # defaults (the _compact_* instance attributes set in __init__).
        if chatflow_compact_trigger_pct is not _UNSET:
            self._effective_compact_trigger_pct = chatflow_compact_trigger_pct  # type: ignore[assignment]
        else:
            self._effective_compact_trigger_pct = self._compact_trigger_pct
        if chatflow_compact_target_pct is not _UNSET:
            self._effective_compact_target_pct = chatflow_compact_target_pct  # type: ignore[assignment]
        else:
            self._effective_compact_target_pct = self._compact_target_pct
        if chatflow_compact_keep_recent_count is not _UNSET:
            self._effective_compact_keep_recent_count = chatflow_compact_keep_recent_count  # type: ignore[assignment]
        else:
            self._effective_compact_keep_recent_count = self._compact_keep_recent_count
        if chatflow_compact_preserve_mode is not _UNSET:
            self._effective_compact_preserve_mode = chatflow_compact_preserve_mode  # type: ignore[assignment]
        else:
            self._effective_compact_preserve_mode = self._compact_preserve_mode
        if chatflow_compact_model is not _UNSET:
            self._effective_compact_model = chatflow_compact_model  # type: ignore[assignment]
        else:
            self._effective_compact_model = self._compact_model
        self._effective_runtime_note = (
            ""
            if chatflow_runtime_environment_note is _UNSET
            else str(chatflow_runtime_environment_note or "")
        )
        if chatflow_max_produced_tags is not _UNSET:
            self._effective_max_produced_tags = int(chatflow_max_produced_tags)  # type: ignore[arg-type]
        if chatflow_max_consumed_tags is not _UNSET:
            self._effective_max_consumed_tags = int(chatflow_max_consumed_tags)  # type: ignore[arg-type]
        self._revise_count = 0
        self._post_node_hook = post_node_hook
        self._disabled_tool_names = disabled_tool_names or frozenset()
        self._chatflow_id = chatflow_id
        broken: set[str] = set()
        done: set[str] = set()

        # Parallel-ready scheduling: each outer pass collects every node
        # whose parents are all in ``done`` and runs the batch
        # concurrently via ``asyncio.gather``. The tool loop and
        # ``post_node_hook`` can mutate the DAG inside ``_run_node`` —
        # any nodes they add land in the next pass's ready set after
        # ``topological_order()`` is recomputed.
        while True:
            order = workflow.topological_order()
            ready: list[WorkFlowNode] = []
            for node_id in order:
                if node_id in done:
                    continue
                node = workflow.get(node_id)

                if node.is_frozen:
                    done.add(node_id)
                    continue

                if any(p in broken for p in node.parent_ids):
                    node.status = NodeStatus.CANCELLED
                    node.error = "skipped: ancestor failed"
                    done.add(node_id)
                    continue

                # Only schedule once every parent has finished this run.
                # Parents that are still planned/running in a later
                # batch will let this node appear in a future pass.
                if not all(p in done for p in node.parent_ids):
                    continue

                # PR A (2026-04-21): judge_post is the WorkFlow's exit
                # gate and needs every sibling NODE-brief rendered into
                # its layer-notes. Briefs used to live in its
                # ``parent_ids``; that violated the "brief has one edge"
                # invariant, so we gate here instead. If any scope=NODE
                # brief is still planned/running, defer judge_post to
                # the next pass. Briefs never FAIL (``_run_brief`` always
                # falls back to a code template), so this terminates.
                if (
                    node.step_kind == StepKind.JUDGE_CALL
                    and node.judge_variant == JudgeVariant.POST
                    and any(
                        n.step_kind == StepKind.BRIEF
                        and n.scope == NodeScope.NODE
                        and n.status
                        in (NodeStatus.PLANNED, NodeStatus.RUNNING)
                        for n in workflow.nodes.values()
                    )
                ):
                    continue

                ready.append(node)

            if not ready:
                break

            await asyncio.gather(
                *(self._run_node(workflow, n) for n in ready)
            )
            for n in ready:
                if n.status == NodeStatus.FAILED:
                    broken.add(n.id)
                if n.status == NodeStatus.PLANNED:
                    # Tier 1 compact deferred this node — leave it out
                    # of ``done`` so the next topological pass picks
                    # it up again once its new compact parent runs.
                    continue
                done.add(n.id)

            # Planner-grounding fuse: once enough leaves have resolved,
            # require tool_calls to occupy at least ``min_ground_ratio``
            # of them. Catches runaway planner/judge churn that never
            # lands a real action (see §5.4 / 2026-04-17 incident).
            if (
                workflow.pending_user_prompt is None
                and self._effective_min_ground_ratio is not None
            ):
                leaves, tools = _compute_ground_ratio(workflow)
                if (
                    leaves >= self._effective_ground_ratio_grace
                    and tools / leaves < self._effective_min_ground_ratio
                ):
                    workflow.pending_user_prompt = format_ground_ratio_halt_prompt(
                        leaves=leaves,
                        tools=tools,
                        min_ratio=self._effective_min_ground_ratio,
                    )
                    log.info(
                        "ground-ratio fuse halt: workflow=%s leaves=%d tools=%d ratio=%.3f threshold=%.3f",
                        workflow.id,
                        leaves,
                        tools,
                        tools / leaves,
                        self._effective_min_ground_ratio,
                    )

            # If a judge pass decided the WorkFlow must bounce back
            # to the ChatFlow layer for user clarification, stop
            # running — remaining planned nodes stay dashed, and
            # the ChatFlow engine opens a new ChatNode whose
            # agent_response is the pending prompt.
            if workflow.pending_user_prompt is not None:
                break

        # Flow-level brief was retired 2026-04-21: the enclosing
        # layer's ChatNode brief (or the outer delegate WorkNode's
        # node-brief, for nested sub-WorkFlows) already summarizes
        # this flow as a single unit, so a second FLOW-scoped pass
        # was redundant. Node-briefs still cover every main-axis
        # WorkNode, including DELEGATE, so the MemoryBoard retains
        # full per-node coverage without the duplicate work.
        await self._bus.publish(
            WorkflowEvent(workflow_id=workflow.id, kind="workflow.completed")
        )
        return workflow

    def _token_callback(
        self, workflow: WorkFlow, node: WorkFlowNode
    ) -> TokenCallback:
        """Build the per-token publish closure handed to the provider.

        Each fragment becomes a ``node.token`` event on the bus. The
        chatflow_engine relay re-publishes it as
        ``chat.workflow.node.token`` so the frontend can render a
        live preview while a slow model (e.g. local 27B Ollama
        loading from cold) is still generating.
        """
        wf_id = workflow.id
        node_id = node.id

        async def publish(piece: str) -> None:
            await self._bus.publish(
                WorkflowEvent(
                    workflow_id=wf_id,
                    kind="node.token",
                    node_id=node_id,
                    data={"delta": piece},
                )
            )

        return publish

    async def _forward_sub_events(
        self,
        sub_id: str,
        parent_id: str,
        queue: asyncio.Queue[WorkflowEvent | None],
    ) -> None:
        """Re-publish ``sub_id``-scoped events under ``parent_id``.

        Preserves ``kind``, ``node_id``, and ``data`` — only the
        ``workflow_id`` changes. Drops ``workflow.completed`` so each
        sub-WorkFlow's internal completion doesn't look like the
        outer run's completion to downstream subscribers.
        """
        async for event in self._bus.drain(sub_id, queue):
            if event.kind == "workflow.completed":
                continue
            await self._bus.publish(
                WorkflowEvent(
                    workflow_id=parent_id,
                    kind=event.kind,
                    node_id=event.node_id,
                    data=event.data,
                )
            )

    async def _run_node(self, workflow: WorkFlow, node: WorkFlowNode) -> None:
        node.status = NodeStatus.RUNNING
        node.started_at = utcnow()
        await self._bus.publish(
            WorkflowEvent(
                workflow_id=workflow.id,
                kind="node.running",
                node_id=node.id,
                data={"step_kind": node.step_kind.value},
            )
        )

        try:
            if node.step_kind == StepKind.DRAFT:
                await self._run_llm_call(workflow, node)
            elif node.step_kind == StepKind.TOOL_CALL:
                await self._run_tool_call(workflow, node)
            elif node.step_kind == StepKind.JUDGE_CALL:
                await self._run_judge_call(workflow, node)
            elif node.step_kind == StepKind.DELEGATE:
                await self._run_sub_agent_delegation(workflow, node)
            elif node.step_kind == StepKind.COMPRESS:
                await self._run_compact(workflow, node)
            elif node.step_kind == StepKind.BRIEF:
                await self._run_brief(workflow, node)
            else:  # pragma: no cover — enum exhaustiveness
                raise ValueError(f"unknown step_kind {node.step_kind}")
        except _CompactRequested:
            # Tier 1 pre-call check spliced a compact WorkNode in front
            # of this one. _insert_compact_worknode already reset the
            # node to PLANNED and added the compact as a new parent;
            # we just need to emit a bus event so subscribers know
            # this run got deferred, then unwind without the
            # "node.failed" treatment.
            await self._bus.publish(
                WorkflowEvent(
                    workflow_id=workflow.id,
                    kind="node.compact_deferred",
                    node_id=node.id,
                    data={"compact_parent": node.parent_ids[0]},
                )
            )
            return
        except Exception as exc:  # noqa: BLE001 — engine boundary
            node.status = NodeStatus.FAILED
            node.error = f"{type(exc).__name__}: {exc}"
            node.finished_at = utcnow()
            await self._bus.publish(
                WorkflowEvent(
                    workflow_id=workflow.id,
                    kind="node.failed",
                    node_id=node.id,
                    data={"error": node.error},
                )
            )
        else:
            # A handler may have already marked the node terminal (e.g.
            # ``_run_sub_agent_delegation`` flips to FAILED when it
            # absorbs a sub-layer halt). Don't overwrite that decision.
            if node.status == NodeStatus.RUNNING:
                node.status = NodeStatus.SUCCEEDED
                node.finished_at = utcnow()
                await self._bus.publish(
                    WorkflowEvent(
                        workflow_id=workflow.id,
                        kind="node.succeeded",
                        node_id=node.id,
                        data={"usage": node.usage.model_dump() if node.usage else None},
                    )
                )
                # MemoryBoard node-brief auto-spawn (PR 1). Gates:
                # (1) ``_board_writer`` is wired (chatflow layer will
                # persist); (2) source kind is briefable. BRIEF is the
                # recursion guard; COMPRESS writes its board_item
                # directly in ``_run_compact`` because the snapshot
                # summary IS the brief (PR 4.2.a — no LLM cost for
                # briefing a summary of a summary). DELEGATE is
                # briefed here too (flow-brief retired 2026-04-21);
                # its source output is derived from the sub-WorkFlow's
                # terminal node by ``_render_source_output_for_brief``.
                if (
                    self._board_writer is not None
                    and node.step_kind != StepKind.BRIEF
                    and node.step_kind != StepKind.COMPRESS
                ):
                    self._spawn_node_brief(workflow, node)
            elif node.status == NodeStatus.FAILED:
                await self._bus.publish(
                    WorkflowEvent(
                        workflow_id=workflow.id,
                        kind="node.failed",
                        node_id=node.id,
                        data={"error": node.error},
                    )
                )

        # Let the caller grow the DAG before the next iteration picks
        # up the new nodes (Option B: judge_pre / llm_call completion
        # decides whether to spawn judge_post or an llm_call follow-up).
        # The hook also fires for FAILED nodes so post_judge crashes can
        # be retried — the hook itself filters which kinds it acts on.
        #
        # Wrapped in try/except because the hook runs arbitrary chat-layer
        # code (spawning planner/worker/judge nodes). A raise here would
        # otherwise bubble up through ``asyncio.gather`` and cancel every
        # sibling in the same batch, leaving the WorkFlow half-built and
        # the chat layer reporting the opaque "no terminal llm_call".
        # The hook has already logged enough to diagnose; swallowing it
        # here lets the driver loop see the next topological pass, where
        # the defensive fallthrough guards (e.g. planner_judge's
        # nodes_before check) fire a halt_to_post_judge so the user gets
        # a real error instead of a stalled WorkFlow.
        if self._post_node_hook is not None:
            try:
                self._post_node_hook(workflow, node)
            except Exception:  # noqa: BLE001 — engine boundary
                log.exception(
                    "post_node_hook crashed on workflow=%s node=%s "
                    "(step_kind=%s role=%s judge_variant=%s status=%s)",
                    workflow.id,
                    node.id,
                    node.step_kind.value if node.step_kind else None,
                    node.role.value if node.role else None,
                    node.judge_variant.value if node.judge_variant else None,
                    node.status.value if node.status else None,
                )

    async def _invoke_and_freeze(
        self,
        workflow: WorkFlow,
        node: WorkFlowNode,
        *,
        expose_tools: bool,
        override_tools: list[ToolDefinition] | None = None,
        extra: dict[str, Any] | None = None,
        json_schema: dict[str, Any] | None = None,
        forced_tool_name: str | None = None,
    ) -> None:
        """Shared provider-call path for ``llm_call`` and ``judge_call``.

        Builds the message context from ``node.input_messages`` or, if
        empty, the ancestor chain; resolves the effective model via
        :func:`effective_model_for`; optionally exposes tool definitions
        (llm_call only — judges don't get tools, see ADR-020); invokes
        the provider and freezes ``output_message`` / ``usage`` onto
        the node. Does **not** spawn a tool loop — that's the caller's
        choice.
        """
        if node.input_messages:
            messages = _wire_to_provider(node.input_messages)
        else:
            messages = _build_context_from_ancestors(workflow, node)

        if not messages:
            raise ValueError(
                f"{node.step_kind.value} node {node.id} has no input_messages "
                "and no ancestor context to build from"
            )

        ref = effective_model_for(workflow, node.id)
        if ref is not None:
            model = f"{ref.provider_id}:{ref.model_id}" if ref.provider_id else ref.model_id
        else:
            model = None

        # Tier 1 compact check — only for ancestor-built contexts and
        # only for the kinds of calls that would be *growing* context.
        # Compact calls themselves are exempt (they're the recovery
        # path; they'd trigger recursion). Judge / planner / worker
        # nodes that were spawned with explicit ``input_messages`` are
        # also skipped: their prompt is template-driven and we have no
        # obvious place to splice in a summary without breaking the
        # template contract. We also exempt nodes whose direct parent
        # is a freshly-settled compact — that's the node the compact
        # was inserted for, and its re-run IS the "compacted version".
        # Re-triggering here would loop indefinitely on pathologically
        # long preserved tails.
        if (
            node.step_kind != StepKind.COMPRESS
            and node.input_messages is None
            and not _parent_is_fresh_compact(workflow, node)
            and self._needs_compact(messages, ref)
        ):
            await self._insert_compact_worknode(workflow, node, messages, ref)
            raise _CompactRequested(node.id)

        # Expose every tool the registry considers visible under this
        # node's constraints. Empty list means "no tools" — stays
        # backward-compatible with M3 callers that don't configure a
        # registry. Judges never see tools even if a registry exists.
        # M7.5: ``resolve_for_node`` folds chatflow-disabled,
        # ``effective_tools`` whitelist, side-effect filter, and the
        # legacy allow/deny ``tool_constraints`` into a single pass.
        # When ``effective_tools is None`` (legacy nodes that pre-date
        # the capability model, or chatflows where judge_pre hasn't
        # populated the field yet) the whitelist step short-circuits
        # to "all tools allowed" and behavior matches the pre-M7.5
        # path.
        tool_defs: list[ToolDefinition] = []
        if override_tools is not None:
            tool_defs = override_tools
        elif expose_tools and self._tools is not None:
            resolved = self._tools.resolve_for_node(
                node_effective=node.effective_tools,
                chatflow_disabled=self._disabled_tool_names,
                side_effect_filter=_side_effect_filter_for(node),
                legacy_constraints=node.tool_constraints,
            )
            tool_defs = [ToolDefinition(**t.definition()) for t in resolved]

        # Runtime environment note — prepended as an extra system message
        # only when this call exposes tools. Pure judge / brief calls
        # (tools=[]) skip the inject so we don't waste tokens on calls
        # that can't tool-call anyway. Empty effective note also skips.
        # Note text is ChatFlowEngine-resolved (static user text +
        # dynamic OS / shell / Bash-disabled hint), passed in via
        # ``execute(chatflow_runtime_environment_note=...)``.
        messages = _maybe_prepend_runtime_note(
            messages, tool_defs, self._effective_runtime_note
        )

        response = await self._provider_call(
            messages,
            tool_defs,
            model,
            on_token=self._token_callback(workflow, node),
            extra=extra,
            json_schema=json_schema,
            forced_tool_name=forced_tool_name,
        )

        # Freeze the result on the node.
        assistant = response.message
        node.output_message = WireMessage(
            role="assistant",
            content=assistant.content or "",
            tool_uses=[
                SchemaToolUse(id=tu.id, name=tu.name, arguments=dict(tu.arguments))
                for tu in assistant.tool_uses
            ],
            extras=dict(assistant.extras) if assistant.extras else {},
        )
        if node.input_messages is None:
            node.input_messages = _provider_to_wire(messages)
        node.usage = TokenUsage(**response.usage.model_dump()) if response.usage else None

    async def _run_llm_call(self, workflow: WorkFlow, node: WorkFlowNode) -> None:
        """Run an llm_call node: invoke the provider, freeze the output,
        and spawn a tool-use loop if the model requested one.

        Context construction rule (ADR-009 / §4.3): the ancestor chain
        only, not the full DAG. We walk topologically and include each
        frozen llm_call ancestor's input (seed) plus its output_message;
        tool_call ancestors contribute a ``tool`` message. If the node
        carries explicit ``input_messages``, we use those as-is.
        """
        # Planner nodes emit a JSON object matching RecursivePlannerOutput.
        # When the downstream provider supports structured output, we
        # pass the Pydantic-derived schema so the wire layer can enforce
        # it (Ollama format:, OpenAI response_format json_schema, etc.).
        # Adapters whose json_mode resolves to "object" will get a plain
        # json_object shape; "none" falls through to prompt-only.
        # Planner nodes must NOT expose tools: the openai_compat adapter
        # silently drops ``response_format`` when ``tools`` is non-empty,
        # so if we expose tools the json_schema enforcement is lost and
        # models fall back to markdown-fenced JSON (which the parser then
        # has to heuristically unwrap). Planners are pure "decide how to
        # decompose" nodes — they never actually call a tool — so tools
        # can safely be suppressed on this path.
        is_planner = node.role == WorkNodeRole.PLAN
        # Use the discriminated-oneOf schema (mode→body cross-field
        # constraint statically encoded) for the wire — the
        # Pydantic-derived schema only marks ``mode`` required and is
        # too permissive for grammar-constrained decoders. Pydantic's
        # runtime validator still catches any output that slips
        # through. See ``planner_grammar_schema``.
        planner_schema: dict[str, Any] | None = (
            planner_grammar_schema() if is_planner else None
        )
        await self._invoke_and_freeze(
            workflow, node, expose_tools=not is_planner, json_schema=planner_schema
        )
        assert node.output_message is not None

        # ------------------------------------------------------------- tool loop
        # If the model requested tool calls AND we have a registry
        # configured, auto-spawn child tool_call nodes + a follow-up
        # llm_call to feed the results back. The outer execute() loop
        # will pick up the newly-planned children on its next pass.
        if self._tools is not None and node.output_message.tool_uses:
            _assert_tool_loop_budget(workflow, node, self._effective_budget)
            _spawn_tool_loop_children(workflow, node)

    # --------------------------------------------------------------- compact

    def _context_window_for(self, ref: ProviderModelRef | None) -> int:
        """Resolve the model's context window in tokens, falling back
        to :data:`DEFAULT_CONTEXT_WINDOW_TOKENS` when the lookup isn't
        plumbed in or returns ``None``.
        """
        if self._context_window_lookup is not None:
            resolved = self._context_window_lookup(ref)
            if resolved is not None and resolved > 0:
                return resolved
        return DEFAULT_CONTEXT_WINDOW_TOKENS

    def _needs_compact(
        self,
        messages: list[Message],
        ref: ProviderModelRef | None,
    ) -> bool:
        """Return True iff the estimated footprint of *messages*
        crosses the configured fraction of the target model's context
        window. Pure read — never mutates engine or workflow state.

        ``chatflow_compact_trigger_pct=None`` (either at the ChatFlow
        level or the engine default) disables Tier 1 entirely for this
        run.
        """
        trigger = self._effective_compact_trigger_pct
        if trigger is None:
            return False
        estimated = _estimate_tokens_from_provider_messages(messages)
        ctx = self._context_window_for(ref)
        threshold = int(ctx * trigger)
        return estimated >= threshold

    async def _insert_compact_worknode(
        self,
        workflow: WorkFlow,
        node: WorkFlowNode,
        messages: list[Message],
        ref: ProviderModelRef | None,
    ) -> WorkFlowNode:
        """Splice a COMPACT WorkNode in front of *node* and re-parent
        *node* onto it.

        - Carves off the last ``compact_keep_recent_count`` provider
          messages as verbatim tail.
        - Serializes the remaining head into the compact worker's
          ``messages_to_compact`` param.
        - Instantiates :file:`compact.yaml` to borrow the rendered
          prompt, then builds a single ``StepKind.COMPRESS`` node with
          that prompt and a pre-populated snapshot holding the
          preserved tail + accounting.
        - ``node.parent_ids`` become ``[compact.id]``; the compact
          inherits ``node``'s prior parents. The engine will pick up
          the compact on its next ready pass; when the compact
          finishes, *node* will run again and build its context from
          the snapshot.

        Brief-fallback (2026-04-23): when the serialized head would
        overflow the compact model's own context window (e.g. user
        configured a small ``compact_model`` while main turns run on a
        larger model), replace raw ancestor messages with their
        MemoryBoard briefs + node_id citations before serialization.
        Downstream readers can still drill down via ``get_node_context``,
        matching the Pack citation pattern. Without this the compact
        worker would 500 on prompt-too-long and the parent node would
        never re-run — which is ironic since the whole point of compact
        is to prevent that.
        """
        from agentloom.templates.instantiate import instantiate_fixture

        tagged = _build_tagged_context_from_ancestors(workflow, node)
        # by_budget: summary first, tail is packed post-hoc in _run_compact,
        # so feed the full ancestry to the summarizer (keep=0).
        if self._effective_compact_preserve_mode == "by_budget":
            keep = 0
        else:
            keep = max(
                0,
                min(len(tagged), self._effective_compact_keep_recent_count),
            )
        head_tagged = tagged[:-keep] if keep else tagged
        tail = [m for _, m in tagged[-keep:]] if keep else []
        head_serialized = _render_messages_to_compact(head_tagged)
        original_tokens = _estimate_tokens_from_provider_messages(messages)
        ctx = self._context_window_for(ref)
        target_tokens = max(256, int(ctx * self._effective_compact_target_pct))

        compact_model = self._effective_compact_model or node.model_override or ref
        # Preflight: if the serialized head would overflow the compact
        # model's own window, swap raw messages for brief+id citations.
        # 0.9 leaves headroom for the compact fixture's system prompt +
        # target-summary budget (the model still needs tokens to think
        # + emit).
        compact_ctx = self._context_window_for(compact_model)
        head_tokens_est = _count_text_tokens(head_serialized)
        if head_tokens_est > compact_ctx * 0.9:
            log.info(
                "compact head overflow preflight: workflow=%s pending=%s "
                "est_head_tokens=%d compact_ctx=%d — falling back to "
                "brief+id citation form",
                workflow.id,
                node.id,
                head_tokens_est,
                compact_ctx,
            )
            briefs_by_node = await self._load_briefs_for_workflow(workflow.id)
            head_tagged = _rewrite_tagged_as_briefs(head_tagged, briefs_by_node)
            head_serialized = _render_messages_to_compact(head_tagged)

        compact_plan, includes = _get_compact_fixture()
        compact_wf = instantiate_fixture(
            compact_plan,
            {
                "messages_to_compact": head_serialized,
                "target_tokens": target_tokens,
                "must_keep": "",
                "must_drop": "",
                "compact_instruction": "",
            },
            includes=includes,
        )
        # The compact plan ships as a single-node WorkFlow; we borrow
        # its fully-rendered input_messages (system + user prompt).
        (inner,) = compact_wf.nodes.values()
        assert inner.input_messages is not None
        prompt = list(inner.input_messages)

        preserved_wire = _provider_to_wire(tail)
        snapshot = CompactSnapshot(
            summary="",  # filled in by _run_compact after the LLM call
            preserved_messages=preserved_wire,
        )
        compact_node = WorkFlowNode(
            step_kind=StepKind.COMPRESS,
            parent_ids=list(node.parent_ids),
            description=_compact_description_text(),
            input_messages=prompt,
            compact_snapshot=snapshot,
            model_override=compact_model,
            resolved_model=compact_model,
        )
        workflow.add_node(compact_node)
        if compact_node.id in workflow.root_ids and compact_node.parent_ids:
            # add_node appended to root_ids under the assumption of an
            # empty parent list; revert since we carry real parents.
            workflow.root_ids.remove(compact_node.id)
        node.parent_ids = [compact_node.id]
        # Reset the pending node so execute() re-schedules it after the
        # compact finishes.
        node.status = NodeStatus.PLANNED
        node.started_at = None
        log.info(
            "compact inserted: workflow=%s pending_node=%s compact=%s "
            "original_tokens=%d target_tokens=%d preserved=%d",
            workflow.id,
            node.id,
            compact_node.id,
            original_tokens,
            target_tokens,
            len(tail),
        )
        return compact_node

    async def _run_compact(self, workflow: WorkFlow, node: WorkFlowNode) -> None:
        """Run a COMPACT WorkNode: invoke the provider on the rendered
        prompt the engine pre-filled and splice the model's summary
        into ``compact_snapshot``.
        """
        await self._invoke_and_freeze(workflow, node, expose_tools=False)
        assert node.output_message is not None
        summary = (node.output_message.content or "").strip()
        if node.compact_snapshot is None:
            # Shouldn't happen — _insert_compact_worknode always
            # pre-populates. Be defensive so a malformed node fails
            # loudly rather than silently producing an empty snapshot.
            raise RuntimeError(
                f"compact node {node.id} missing pre-populated snapshot"
            )
        # Summary handling splits by preserve-mode:
        # - by_count: target_pct is advisory on the preserve side; the
        #   LLM's summary is kept as-is (N tail messages already carved
        #   off at insert time). No hard cap — the invariant relies on
        #   the fixed-N tail, not on a summary bound.
        # - by_budget: target_pct is the *total* budget for the compact
        #   region. We greedy-pack the tail from ancestors into whatever
        #   is left after the summary's tokens. We still hard-cap the
        #   summary itself at target × ctx as a safety net: if the
        #   summarizer runs long, overflow reads as "no tail preserved"
        #   which is already a degenerate case; letting the summary
        #   itself blow past the window would strand the next turn.
        ctx = self._context_window_for(node.resolved_model or node.model_override)
        mode = self._effective_compact_preserve_mode
        summary_tokens = _count_text_tokens(summary)
        if mode == "by_budget":
            target_tokens = max(256, int(ctx * self._effective_compact_target_pct))
            if summary_tokens > target_tokens:
                log.warning(
                    "compact summary exceeds target: node=%s summary_tokens=%d "
                    "target_tokens=%d — truncating",
                    node.id,
                    summary_tokens,
                    target_tokens,
                )
                summary = _truncate_text_to_tokens(summary, target_tokens)
                summary_tokens = _count_text_tokens(summary)
            # Greedy knapsack from the tail: we want the *newest*
            # messages, so iterate the ancestor chain in reverse and
            # admit each as long as cumulative tokens fit in the
            # remaining budget. Ancestor chain is re-derived from the
            # compact node (its own parent_ids still hold the original
            # chain — insertion re-parented the downstream node onto
            # us, not us onto anyone new).
            tagged = _build_tagged_context_from_ancestors(workflow, node)
            packed = _greedy_pack_tail_within_budget(
                [msg for _, msg in tagged], target_tokens - summary_tokens
            )
            preserved_wire = _provider_to_wire(packed)
            node.compact_snapshot = node.compact_snapshot.model_copy(
                update={"summary": summary, "preserved_messages": preserved_wire}
            )
        else:
            node.compact_snapshot = node.compact_snapshot.model_copy(
                update={"summary": summary}
            )
        # PR 4.2.a: the compact's summary IS a MemoryBoard brief — write
        # the board_item directly instead of spawning a secondary BRIEF
        # WorkNode to summarize the summary.
        await self._persist_board_item(
            workflow=workflow,
            source=node,
            scope=NodeScope.NODE,
            description=summary,
            fallback=False,
        )

    # --------------------------------------------------------------- brief (MemoryBoard PR 1)

    async def _render_node_briefs_from_board(self, workflow_id: str) -> str:
        """Pull scope=node MemoryBoard rows for *workflow_id* and format
        them into the prose block judge_post's layer-notes expects —
        one line per row as ``[<source_kind>] <description>``.

        PR A (2026-04-21): judge_post's layer-notes used to walk
        ``workflow.nodes`` and pick out BRIEF WorkNodes. That treated
        the brief WorkNode as a synthetic parent of its consumer — the
        exact architectural deviation PR A is undoing. Read the same
        content from MemoryBoard instead so briefs keep a single-edge
        topology. Node ids are deliberately NOT rendered; the system
        tracks source_node_id in board metadata so descriptions stay
        id-free. (flow_brief was retired in the same pass; this helper
        now has a single consumer.)
        """
        if self._board_reader is None:
            return ""
        rows = await self._board_reader(workflow_id=workflow_id)
        lines: list[str] = []
        for row in rows:
            if row.get("scope") != NodeScope.NODE.value:
                continue
            kind = row.get("source_kind") or "?"
            desc = (row.get("description") or "").strip().replace("\n", " ")
            if not desc:
                continue
            lines.append(f"[{kind}] {desc}")
        return "\n".join(lines)

    async def _load_briefs_for_workflow(
        self, workflow_id: str
    ) -> dict[str, str]:
        """Return ``{source_node_id: one_line_brief}`` for every
        ``scope=node`` row on *workflow_id*. Used by the compact
        brief-fallback path (``_insert_compact_worknode``) so an
        oversize head can be rewritten as ``[node:<id>] <brief>``
        citations instead of raw content. Empty dict when the engine
        has no ``board_reader`` wired (bare tests / pre-PR1 runs).
        """
        if self._board_reader is None:
            return {}
        rows = await self._board_reader(workflow_id=workflow_id)
        out: dict[str, str] = {}
        for row in rows:
            if row.get("scope") != NodeScope.NODE.value:
                continue
            sid = row.get("source_node_id")
            desc = (row.get("description") or "").strip().replace("\n", " ")
            if sid and desc:
                out[sid] = desc
        return out

    async def _inject_layer_notes_for_post_judge(
        self, workflow: WorkFlow, node: WorkFlowNode
    ) -> None:
        """Append a ``Layer notes`` system message to a POST judge's input.

        Replaces the ADR-era spawn-time ``shared_notes`` rendering (PR
        4.2.c) and the short-lived DAG-walk renderer (PR 4.2.c → PR A).
        Briefs are scope=NODE WorkNodes with exactly one edge to their
        source; the scheduler gates this judge_post's ready state on
        every sibling brief reaching a terminal status (see the ready
        loop's BRIEF gate), so by the time this runs the board rows
        we read are the full post-hoc trail. Idempotent via
        ``_LAYER_NOTES_MARKER``: a crash-clone's copied input_messages
        already carries the block and we must not duplicate.
        """
        if not node.input_messages:
            return
        for msg in node.input_messages:
            if msg.role == "system" and _LAYER_NOTES_MARKER in msg.content:
                return
        rendered = await self._render_node_briefs_from_board(workflow.id)
        if not rendered:
            return
        body = (
            f"{_LAYER_NOTES_MARKER}\n"
            "Layer notes (sibling WorkNode briefs; cite ids in redo_targets)\n"
            "---------------------------------------------------------------\n"
            f"{rendered}"
        )
        node.input_messages = [
            *node.input_messages,
            WireMessage(role="system", content=body),
        ]

    def _spawn_node_brief(self, workflow: WorkFlow, source: WorkFlowNode) -> WorkFlowNode:
        """Attach a ``scope=NODE`` brief WorkNode whose only parent is
        the freshly-succeeded *source*.

        The brief is added in PLANNED state — the outer loop picks it
        up on its next ready pass. tool_call sources get an
        ``input_messages=None`` brief (``_run_brief`` short-circuits
        before needing fixtures); other kinds get a fully rendered
        prompt so the LLM path can run.
        """
        from agentloom.templates.instantiate import instantiate_fixture

        brief_model = (
            workflow.brief_model_override
            or source.resolved_model
            or source.model_override
            # DELEGATE nodes never invoke an LLM on their own axis, so
            # ``resolved_model`` is always None for them; fall back to
            # any main-axis sibling that *did* resolve a model so the
            # brief still runs via the LLM path instead of the code
            # template (which would just say "delegate: <id>").
            or _any_resolved_model(workflow)
        )

        if source.step_kind == StepKind.TOOL_CALL or brief_model is None:
            # Deterministic code template — no LLM, so no fixture
            # rendering needed. ``_run_brief`` sees
            # ``input_messages is None`` and bypasses the provider
            # entirely. Also taken when no ``brief_model`` is
            # resolvable (e.g. bare in-memory engines in tests, or any
            # chatflow with no draft/brief model configured): brief
            # remains always-on as a WorkFlow node, but skips the LLM
            # it has no model for.
            brief = WorkFlowNode(
                step_kind=StepKind.BRIEF,
                scope=NodeScope.NODE,
                parent_ids=[source.id],
                description=_brief_description_text(NodeScope.NODE),
                model_override=brief_model,
                resolved_model=brief_model,
            )
        else:
            plan, includes = _get_brief_fixture("node_brief")
            inputs_rendered = _render_source_inputs_for_brief(source)
            output_text = _render_source_output_for_brief(source)
            # ``source.description`` is deliberately NOT forwarded: the
            # pre-check trio (description / inputs / expected_outcome)
            # lives on the parent WorkFlow and must not leak into a
            # single WorkNode's brief. Source kind + rendered inputs
            # and outputs are enough for the summarizer.
            brief_wf = instantiate_fixture(
                plan,
                {
                    "source_kind": source.step_kind.value,
                    "source_inputs": inputs_rendered,
                    "source_output": output_text,
                    "max_produced_tags": self._effective_max_produced_tags,
                    "max_consumed_tags": self._effective_max_consumed_tags,
                    # Node-level ancestral anchor lookup is deferred —
                    # it'd require a board_reader chain walk per spawn,
                    # which the engine doesn't currently do. Pass
                    # empty so the fixture's anchor block falls back
                    # to "(none)". Chat-level ancestral tracking
                    # (where the user's main concern lives) IS done in
                    # ``_collect_chat_ancestral_active_tags``.
                    "ancestral_tags_active": "",
                },
                includes=includes,
            )
            (inner,) = brief_wf.nodes.values()
            assert inner.input_messages is not None
            prompt = list(inner.input_messages)
            brief = WorkFlowNode(
                step_kind=StepKind.BRIEF,
                scope=NodeScope.NODE,
                parent_ids=[source.id],
                description=_brief_description_text(NodeScope.NODE),
                input_messages=prompt,
                model_override=brief_model,
                resolved_model=brief_model,
            )

        workflow.add_node(brief)
        # ``add_node`` adds to ``root_ids`` on an empty parent list;
        # briefs always have a real parent, so never register them as
        # roots even though the schema permits it.
        if brief.id in workflow.root_ids:
            workflow.root_ids.remove(brief.id)
        return brief

    async def _run_brief(self, workflow: WorkFlow, node: WorkFlowNode) -> None:
        """Run a BRIEF WorkNode.

        Two code paths:

        - ``input_messages is None`` (tool_call source or pre-rendered
          fallback): skip the LLM entirely. Derive the description
          from the deterministic code template, mark ``fallback=True``
          on the BoardItem, and freeze the node as SUCCEEDED with a
          synthetic ``output_message``.
        - otherwise: invoke the provider via ``_invoke_and_freeze``.
          On LLM failure, fall back to the code template and still
          produce a SUCCEEDED node — the brief never propagates its
          own failure to the source; it's a best-effort summary.

        Persists one BoardItem row via ``self._board_writer``. The
        writer is responsible for its own idempotence (upsert keyed by
        ``source_node_id``).
        """
        if node.scope is None:
            raise ValueError(f"brief node {node.id} missing scope")
        source_id = node.parent_ids[0] if node.parent_ids else None
        if source_id is None or source_id not in workflow.nodes:
            raise ValueError(
                f"brief node {node.id} has no source parent in this workflow"
            )
        source = workflow.get(source_id)

        description: str
        fallback: bool
        produced_tags: list[str] = []
        consumed_tags: list[str] = []

        if node.input_messages is None:
            # tool_call source or fallback pre-arranged by the caller.
            description = _brief_code_template_for_node(source)
            fallback = True
            # Synthesize an output_message so the node looks like a
            # normal completed brief from the outside. No usage, no
            # LLM call was made.
            node.output_message = WireMessage(role="assistant", content=description)
        else:
            from agentloom.engine.brief_parser import (
                brief_grammar_schema,
                parse_brief_output,
            )

            try:
                await self._invoke_and_freeze(
                    workflow,
                    node,
                    expose_tools=False,
                    json_schema=brief_grammar_schema(),
                )
                assert node.output_message is not None
                raw = node.output_message.content or ""
                if not raw.strip():
                    description = _brief_code_template_for_node(source)
                    fallback = True
                    node.output_message = WireMessage(
                        role="assistant", content=description
                    )
                else:
                    parsed = parse_brief_output(raw)
                    description = parsed.description
                    produced_tags = parsed.produced_tags[
                        : self._effective_max_produced_tags
                    ]
                    consumed_tags = parsed.consumed_tags[
                        : self._effective_max_consumed_tags
                    ]
                    fallback = False
            except Exception as exc:  # noqa: BLE001 — brief never fails
                log.warning(
                    "brief LLM call failed for source=%s — falling back to "
                    "code template: %s",
                    source.id,
                    exc,
                )
                description = _brief_code_template_for_node(source)
                fallback = True
                node.output_message = WireMessage(
                    role="assistant", content=description
                )

        await self._persist_board_item(
            workflow=workflow,
            source=source,
            scope=node.scope,
            description=description,
            fallback=fallback,
            produced_tags=produced_tags,
            consumed_tags=consumed_tags,
        )

    async def _persist_board_item(
        self,
        *,
        workflow: WorkFlow,
        source: WorkFlowNode,
        scope: NodeScope,
        description: str,
        fallback: bool,
        produced_tags: list[str] | None = None,
        consumed_tags: list[str] | None = None,
    ) -> None:
        """Handoff to ``self._board_writer`` for a node-scope brief.

        Silently no-ops when no writer is configured — tests run bare
        engines without a DB session, and we must not synthesize board
        rows when there's nowhere to put them.
        """
        if self._board_writer is None:
            return
        try:
            await self._board_writer(
                chatflow_id=self._chatflow_id,
                workflow_id=workflow.id,
                source_node_id=source.id,
                source_kind=source.step_kind.value,
                scope=scope.value,
                description=description,
                fallback=fallback,
                produced_tags=produced_tags,
                consumed_tags=consumed_tags,
            )
        except Exception:  # noqa: BLE001 — board is best-effort
            log.exception(
                "board_writer failed for source=%s scope=%s — brief text stays "
                "on the WorkNode but no BoardItem row was written",
                source.id,
                scope.value,
            )

    async def _run_sub_agent_delegation(
        self, workflow: WorkFlow, node: WorkFlowNode
    ) -> None:
        """Execute the delegation's sub-WorkFlow recursively.

        Spawns a fresh :class:`WorkflowEngine` for the recursive
        ``execute()`` so per-call state (budgets, revise counter,
        disabled-tool filter, post-node hook) lives on its own instance
        rather than on ``self``. This is what lets sibling
        sub_agent_delegations run concurrently under ``asyncio.gather``
        without clobbering each other's counters — a single engine's
        save/restore pattern is not safe across parallel awaits. Same
        provider, bus, tool registry, and tool context are shared; the
        outer-resolved budgets are passed as the inner's "chatflow
        defaults" so a sub-WorkFlow without its own override inherits
        the running effective values.

        SSE forwarding: the sub engine publishes its node events on
        ``sub.id`` (its own ``workflow_id``), but the ChatFlow-level
        relay only subscribes to the outermost WorkFlow's id. Without
        a forwarder the frontend would see nothing inside any
        ``sub_agent_delegation`` — pre/planner/judge/etc. would all
        be invisible until the next full-snapshot refresh. We open a
        subscription to ``sub.id`` and re-publish every event under
        the outer ``workflow.id``. Nested delegations chain through:
        sub_2 → sub_1 → outer → ChatFlow relay. ``workflow.completed``
        is dropped so only the outermost completion reaches the
        ChatFlow layer.
        """
        sub = node.sub_workflow
        if sub is None:
            raise ValueError(
                f"sub_agent_delegation {node.id} has no sub_workflow"
            )

        sub_engine = WorkflowEngine(
            self._provider_call,
            self._bus,
            self._tools,
            self._tool_ctx,
            context_window_lookup=self._context_window_lookup,
            compact_trigger_pct=self._effective_compact_trigger_pct,
            compact_target_pct=self._effective_compact_target_pct,
            compact_keep_recent_count=self._effective_compact_keep_recent_count,
            compact_model=self._effective_compact_model,
            board_writer=self._board_writer,
            board_reader=self._board_reader,
        )
        forward_queue = self._bus.open_subscription(sub.id)
        forward_task = asyncio.create_task(
            self._forward_sub_events(sub.id, workflow.id, forward_queue),
            name=f"forward-{sub.id}",
        )
        try:
            await sub_engine.execute(
                sub,
                chatflow_tool_loop_budget=self._effective_budget,
                chatflow_auto_mode_revise_budget=self._effective_revise_budget,
                chatflow_min_ground_ratio=self._effective_min_ground_ratio,
                chatflow_ground_ratio_grace_nodes=self._effective_ground_ratio_grace,
                chatflow_id=self._chatflow_id,
                post_node_hook=self._post_node_hook,
                disabled_tool_names=self._disabled_tool_names,
            )
        finally:
            # execute() itself publishes ``workflow.completed`` on
            # ``sub.id`` at the end. Signal end-of-stream so the
            # forwarder drains the tail (including that completed
            # event, which it filters out) and exits naturally. A
            # cancel() here would race the last-batch events and
            # drop them.
            await self._bus.close(sub.id)
            try:
                await forward_task
            except Exception:  # noqa: BLE001 — forwarder must not raise into run loop
                pass

        # Absorb sub-layer halt signals into this delegation node
        # instead of bubbling. The outer ChatNode-level judge is the
        # sole user-facing halt authority (Phase 1 of the 2026-04-14
        # redesign). The delegation node is marked FAILED so the outer
        # aggregating judge_post sees a structured failure via
        # ``_classify_sub_outcome`` / ``_format_decompose_aggregation``
        # and can choose to partial-aggregate, retry, or escalate.
        # ``sub.pending_user_prompt`` is cleared so only the outermost
        # WorkFlow may carry a user-facing prompt.
        if sub.pending_user_prompt is not None:
            log.info(
                "sub-WorkFlow halt bubbling up: parent=%s sub=%s node=%s",
                workflow.id,
                sub.id,
                node.id,
            )
            node.error = f"sub-WorkFlow halted: {sub.pending_user_prompt}"
            node.status = NodeStatus.FAILED
            node.finished_at = utcnow()
            sub.pending_user_prompt = None

    async def _run_tool_call(self, workflow: WorkFlow, node: WorkFlowNode) -> None:
        """Execute a single tool_call node. Requires a registry."""
        if self._tools is None:
            raise RuntimeError(
                "tool_call node encountered but engine has no tool_registry"
            )
        if not node.tool_name:
            raise ValueError(f"tool_call node {node.id} has no tool_name")
        if node.tool_name in self._disabled_tool_names:
            # Defensive: the LLM never sees disabled tools in the prompt,
            # but a hallucinated tool_use still lands here. Surface a
            # normal tool failure so the model can apologize on retry.
            from agentloom.schemas.common import ToolResult

            node.tool_result = ToolResult(
                content=(
                    f"tool {node.tool_name!r} is not enabled for this chatflow"
                ),
                is_error=True,
            )
            return
        result = await self._tools.execute(
            node.tool_name,
            dict(node.tool_args or {}),
            self._tool_ctx,
            constraints=node.tool_constraints,
        )
        node.tool_result = result

    async def _run_judge_call(self, workflow: WorkFlow, node: WorkFlowNode) -> None:
        """Invoke the LLM exactly like an llm_call, then parse the raw
        assistant reply into a :class:`JudgeVerdict` that matches the
        node's declared ``judge_variant`` (ADR-018).

        Parse failures surface as a failed node — the outer ``_run_node``
        marks the status and the raw output is already on
        ``output_message`` for the user to inspect or re-run. The engine
        never silently accepts malformed judge output.

        ``judge_during`` runs in **monitoring mode** for MVP (ADR-020):
        the verdict is written to the node but does not interrupt the
        surrounding WorkFlow. Auto-mode halts on `revise` exhaustion
        and semi_auto's user-driven gates live at the ChatFlow layer.
        """
        if node.judge_variant is None:
            raise ValueError(f"judge_call node {node.id} missing judge_variant")

        # PR A (2026-04-21): judge_post no longer lists sibling briefs in
        # its parent_ids — briefs keep a single-edge topology to their
        # source. The scheduler gates judge_post's ready state on every
        # scope=NODE brief in the WorkFlow reaching a terminal status
        # (see the ready loop), so by the time we reach here the
        # MemoryBoard rows we read are the full post-hoc trail. The
        # rendered block is appended as a trailing system message so
        # the exit gate sees the actual sibling trail.
        if node.judge_variant == JudgeVariant.POST:
            await self._inject_layer_notes_for_post_judge(workflow, node)

        tool_def = judge_verdict_tool_def(node.judge_variant)
        # Defense-in-depth for judge structured output (ADR 2026-04-18):
        # - ``forced_tool_name`` pins the model to the judge_verdict tool
        #   via tool_choice so it can't silently emit a free-text content
        #   reply. Observed failure: ark-code-latest on volcengine with
        #   long prompts + thinking mode ignored the tool and replied
        #   with an invented JSON shape missing ``feasibility``.
        # - ``json_schema`` lets providers that coexist tools with
        #   response_format (openai, volcengine) double-enforce the
        #   verdict shape at the content level. Adapters that can't do
        #   both will silently drop the response_format — tool_choice
        #   still does the real work.
        await self._invoke_and_freeze(
            workflow,
            node,
            expose_tools=False,
            override_tools=[tool_def],
            forced_tool_name="judge_verdict",
            json_schema=judge_verdict_json_schema(node.judge_variant),
        )
        assert node.output_message is not None

        # Prefer tool_use arguments (structured); fall back to content parsing.
        tool_uses = node.output_message.tool_uses or []
        judge_tool = next((tu for tu in tool_uses if tu.name == "judge_verdict"), None)

        # Up to _JUDGE_PARSE_MAX_RETRIES follow-up attempts before the
        # engine bails. ark-code-latest is known to return empty /
        # non-JSON content on the first try under schema pressure and
        # sometimes reproduces the same failure on a single-shot retry —
        # a second retry with fresh context clears the pathology ~90%
        # of the time per 2026-04-22 regression (#2). Each retry costs
        # one extra provider call but the alternative is the whole
        # sub-WorkFlow bubbling up a RuntimeError, which wastes
        # significantly more upstream work.
        parse_errors: list[JudgeParseError] = []
        try:
            if judge_tool is not None:
                node.judge_verdict = parse_judge_from_tool_args(
                    dict(judge_tool.arguments), node.judge_variant,
                )
            else:
                node.judge_verdict = parse_judge_verdict(
                    node.output_message.content, node.judge_variant,
                )
        except JudgeParseError as first_exc:
            parse_errors.append(first_exc)
            for attempt in range(_JUDGE_PARSE_MAX_RETRIES):
                # Exponential backoff before each retry (0.5s, 1s, ...)
                # so transient provider issues — 429, content-filter
                # hiccups, thinking-mode truncation — have a window to
                # clear. No backoff on the very first retry would
                # re-hit the same transient state.
                delay = _JUDGE_PARSE_RETRY_BASE_DELAY * (2 ** attempt)
                await asyncio.sleep(delay)
                try:
                    await self._retry_judge_parse(
                        workflow, node, parse_errors[-1]
                    )
                    break
                except JudgeParseError as retry_exc:
                    parse_errors.append(retry_exc)
                    if attempt == _JUDGE_PARSE_MAX_RETRIES - 1:
                        chain = "; ".join(
                            f"attempt{i}={str(e)[:200]}"
                            for i, e in enumerate(parse_errors)
                        )
                        raise RuntimeError(
                            f"judge parse failed after "
                            f"{_JUDGE_PARSE_MAX_RETRIES} retries: {chain}"
                        ) from retry_exc

        # Option B: judge_post is the WorkFlow's universal exit gate —
        # only it writes ``pending_user_prompt``. judge_pre's verdict
        # is consumed by the post-node hook (set by ChatFlowEngine),
        # which decides whether to spawn an llm_call or route straight
        # to a halt-mode judge_post. judge_during stays monitoring-only
        # except for the auto-mode revise budget halt below.
        verdict = node.judge_verdict
        if node.judge_variant == JudgeVariant.POST and judge_post_needs_user_input(verdict):
            # Retry + redo_targets is the hook's responsibility: the
            # post-node hook re-spawns the targeted nodes and schedules
            # re-aggregation. Only if the hook decides the retry budget
            # is exhausted (or redo_targets is empty) does
            # ``pending_user_prompt`` get set — by the hook itself.
            if verdict.post_verdict == "retry" and verdict.redo_targets:
                pass
            else:
                workflow.pending_user_prompt = format_judge_post_prompt(verdict)
        elif node.judge_variant == JudgeVariant.DURING and verdict.during_verdict == "revise":
            # Monitoring mode (ADR-020) — the WorkFlow keeps running on
            # a single "revise", but auto-mode maintains a running count
            # of revises across this execute() call. Once the count
            # exceeds the budget we halt and bounce back to the user
            # (§5.3 FR-PL-7).
            self._revise_count += 1
            budget = self._effective_revise_budget
            if budget is not None and self._revise_count > budget:
                log.info(
                    "revise-budget halt: workflow=%s revise_count=%d budget=%d",
                    workflow.id,
                    self._revise_count,
                    budget,
                )
                workflow.pending_user_prompt = format_revise_budget_halt_prompt(
                    revise_count=self._revise_count,
                    budget=budget,
                    latest_verdict=verdict,
                )

    async def _retry_judge_parse(
        self,
        workflow: WorkFlow,
        node: WorkFlowNode,
        first_exc: JudgeParseError,
    ) -> None:
        """Re-invoke the judge with a JSON-discipline reminder.

        On success, overwrites ``node.output_message`` and sets
        ``node.judge_verdict``. On failure, re-raises
        :class:`JudgeParseError` for the caller to surface.
        """
        assert node.output_message is not None  # _run_judge_call guarantees
        assert node.judge_variant is not None
        first_raw = node.output_message.content

        # If the first-attempt failure came from a tool_use whose
        # arguments could not be decoded as JSON (``{"_raw": "..."}``
        # sentinel — observed with ark-code-latest emitting Chinese
        # strings with unescaped inner ``"``), surface the broken raw
        # payload verbatim in the retry prompt and give an escape-
        # specific hint. The generic "invalid JSON" message alone was
        # not enough — ark retried the same malformed shape.
        broken_tool_raw: str | None = None
        for tu in node.output_message.tool_uses or []:
            if tu.name == "judge_verdict":
                raw = tu.arguments.get("_raw")
                if isinstance(raw, str) and raw:
                    broken_tool_raw = raw
                break

        if broken_tool_raw is not None:
            correction = (
                "Your previous `judge_verdict` tool call's arguments "
                "were not valid JSON — the raw string you emitted is "
                "reproduced verbatim below between the <RAW> markers:\n"
                "<RAW>\n"
                f"{broken_tool_raw[:1500]}\n"
                "</RAW>\n"
                "Every `\"` character that appears *inside* a JSON "
                "string value MUST be escaped as `\\\"`. Resubmit the "
                "judge_verdict tool call with strictly valid JSON "
                "arguments matching the schema."
            )
        else:
            correction = (
                f"Your previous reply failed JSON parse: {first_exc}. "
                "Reply with ONLY a valid JSON object matching the "
                "required schema — no prose, no code fences, all "
                "string values quoted."
            )

        # Build retry context: original input + the bad response + a
        # terse corrective user message. Keeps the token cost small and
        # shows the model exactly what it emitted.
        base_messages = _wire_to_provider(node.input_messages or [])
        retry_messages: list[Message] = [
            *base_messages,
            AssistantMessage(content=first_raw),
            UserMessage(content=correction),
        ]

        ref = effective_model_for(workflow, node.id)
        model = (
            f"{ref.provider_id}:{ref.model_id}" if ref and ref.provider_id
            else (ref.model_id if ref else None)
        )
        tool_def = judge_verdict_tool_def(node.judge_variant)
        # Retry also forces the tool + ships the schema, for the same
        # reason the first attempt does (see _run_judge_call). Without
        # this the retry would be weaker than the first call whenever
        # the adapter's gating silently dropped tool_choice.
        response = await self._provider_call(
            retry_messages,
            [tool_def],
            model,
            on_token=self._token_callback(workflow, node),
            forced_tool_name="judge_verdict",
            json_schema=judge_verdict_json_schema(node.judge_variant),
        )
        assistant = response.message
        node.output_message = WireMessage(
            role="assistant",
            content=assistant.content or "",
            tool_uses=[
                SchemaToolUse(id=tu.id, name=tu.name, arguments=dict(tu.arguments))
                for tu in assistant.tool_uses
            ],
            extras=dict(assistant.extras) if assistant.extras else {},
        )
        # Accumulate usage — the retry is real provider cost the user
        # should see reflected on the node.
        if response.usage is not None:
            retry_usage = TokenUsage(**response.usage.model_dump())
            if node.usage is None:
                node.usage = retry_usage
            else:
                node.usage = TokenUsage(
                    prompt_tokens=node.usage.prompt_tokens + retry_usage.prompt_tokens,
                    completion_tokens=node.usage.completion_tokens + retry_usage.completion_tokens,
                    total_tokens=node.usage.total_tokens + retry_usage.total_tokens,
                    cached_tokens=node.usage.cached_tokens + retry_usage.cached_tokens,
                    reasoning_tokens=node.usage.reasoning_tokens + retry_usage.reasoning_tokens,
                )

        retry_tool_uses = node.output_message.tool_uses or []
        retry_judge_tool = next((tu for tu in retry_tool_uses if tu.name == "judge_verdict"), None)
        if retry_judge_tool is not None:
            node.judge_verdict = parse_judge_from_tool_args(
                dict(retry_judge_tool.arguments), node.judge_variant,
            )
        else:
            node.judge_verdict = parse_judge_verdict(
                node.output_message.content, node.judge_variant,
            )




def _assert_tool_loop_budget(
    workflow: WorkFlow,
    node: WorkFlowNode,
    effective_budget: int | None,
) -> None:
    """Count how many llm_call ancestors exist; refuse to spawn more
    children if we've already hit the safety cap.

    The count is a cheap heuristic for "how many tool-use iterations
    have we done in this chain" — each loop turn adds exactly one
    llm_call to the ancestor chain, so len(llm ancestors) is the loop
    iteration count.

    ``effective_budget=None`` means unlimited (this check becomes a
    no-op). See :func:`_effective_tool_loop_budget`.
    """
    if effective_budget is None:
        return
    ancestors = workflow.ancestors(node.id) + [node.id]
    llm_ancestors = sum(
        1
        for nid in ancestors
        if workflow.get(nid).step_kind == StepKind.DRAFT
    )
    if llm_ancestors >= effective_budget:
        raise RuntimeError(
            f"tool-use loop exceeded budget ({effective_budget} iterations); "
            "aborting to protect against runaway agents"
        )


def _compute_ground_ratio(workflow: WorkFlow) -> tuple[int, int]:
    """Count this WorkFlow's *completed* leaves for the grounding fuse.

    Returns ``(leaves, tool_calls)`` where ``leaves`` is the number of
    terminal-status non-``sub_agent_delegation`` nodes and
    ``tool_calls`` is the subset of those whose step_kind is
    ``tool_call``.

    Why local-only (no recursion into sub_workflows)? Each recursive
    engine level already runs the check on its own WorkFlow — so a
    sub_agent_delegation whose inner tree is churning halts *inside*
    itself and bubbles up via the existing sub-halt mechanism. The
    outer level's count correctly ignores delegation containers
    (they're not leaves) so a healthy parent that's only dispatching
    to children never trips the fuse.
    """
    leaves = 0
    tools = 0
    for node in workflow.nodes.values():
        if node.step_kind == StepKind.DELEGATE:
            continue
        # MemoryBoard brief nodes are off-axis: they don't represent
        # real work toward the user's request and must not count as
        # either leaves or grounding tool_calls. Without this guard
        # auto-brief spawning would skew the fuse ratio heavily on
        # any WorkFlow with an active board_writer.
        if node.step_kind == StepKind.BRIEF:
            continue
        if node.status not in (NodeStatus.SUCCEEDED, NodeStatus.FAILED):
            continue
        leaves += 1
        if node.step_kind == StepKind.TOOL_CALL:
            tools += 1
    return leaves, tools


def _effective_revise_budget(
    workflow_budget: int | None,
    chatflow_budget: int | None | object,
) -> int | None:
    """Resolve ``workflow.auto_mode_revise_budget ?? chatflow.auto_mode_revise_budget``.

    Mirror of :func:`_effective_tool_loop_budget` but with a different
    fallback: when no ChatFlow context is provided (bare engine test),
    auto-mode defaults to **unlimited** rather than a numeric cap. The
    WorkFlow/ChatFlow-level defaults (``3``) only apply when the caller
    actually hands them in.
    """
    if workflow_budget is not None:
        return workflow_budget
    if chatflow_budget is _UNSET:
        return None  # unlimited for bare engine callers
    assert chatflow_budget is None or isinstance(chatflow_budget, int)
    return chatflow_budget


def _effective_tool_loop_budget(
    workflow_budget: int | None,
    chatflow_budget: int | None | object,
) -> int | None:
    """Resolve ``workflow.tool_loop_budget ?? chatflow.tool_loop_budget``.

    - If the WorkFlow set its own budget, that wins (``None`` on the
      WorkFlow means "inherit from ChatFlow").
    - Else use the ChatFlow's budget. ``None`` there explicitly means
      "unlimited".
    - ``chatflow_budget=_UNSET`` means the caller didn't provide a
      ChatFlow context at all (e.g. a test invoking the engine
      directly); fall back to :data:`MAX_TOOL_LOOP_ITERATIONS` so
      those callers still get a safety cap.
    """
    if workflow_budget is not None:
        return workflow_budget
    if chatflow_budget is _UNSET:
        return MAX_TOOL_LOOP_ITERATIONS
    assert chatflow_budget is None or isinstance(chatflow_budget, int)
    return chatflow_budget


def _spawn_tool_loop_children(workflow: WorkFlow, parent_llm: WorkFlowNode) -> None:
    """Given an llm_call that just emitted tool_uses, append:

    1. One ``tool_call`` WorkFlowNode per tool_use, all as children of
       ``parent_llm``.
    2. One follow-up ``llm_call`` node whose parents are every one of
       the tool_calls just added. Its ``input_messages`` is None so the
       engine will derive context from the ancestor chain on its next
       execute pass.

    Nothing here runs the children — the outer execute() loop picks
    them up on the next iteration.
    """
    assert parent_llm.output_message is not None
    tool_call_ids: list[str] = []
    for tu in parent_llm.output_message.tool_uses:
        tc = WorkFlowNode(
            step_kind=StepKind.TOOL_CALL,
            parent_ids=[parent_llm.id],
            source_tool_use_id=tu.id,
            tool_name=tu.name,
            tool_args=dict(tu.arguments),
            tool_constraints=parent_llm.tool_constraints,
        )
        workflow.add_node(tc)
        tool_call_ids.append(tc.id)

    # Tool-call follow-up llm_calls honor the chatflow-level
    # ``default_tool_call_model`` when the WorkFlow carries one (see
    # WorkFlow.tool_call_model_override). Falls back to the parent
    # llm_call's pin so direct-mode chats keep their existing behavior.
    follow_up = WorkFlowNode(
        step_kind=StepKind.DRAFT,
        parent_ids=tool_call_ids,
        tool_constraints=parent_llm.tool_constraints,
        model_override=workflow.tool_call_model_override or parent_llm.model_override,
    )
    workflow.add_node(follow_up)


def _maybe_prepend_runtime_note(
    messages: list[Message],
    tool_defs: list[ToolDefinition],
    note: str,
) -> list[Message]:
    """Prepend the runtime-environment note as an extra system message
    when the call exposes tools.

    Skips silently when:
    - No tool definitions in this call (judges/briefs without tool
      access don't need the anti-hallucination framing — saves tokens
      and keeps cache prefix stable for those code paths).
    - Note is empty after strip (caller explicitly disabled).
    - First message already IS the same runtime note (dedup): the
      previous LLM call's saved ``input_messages`` already carry it,
      and ``_build_tagged_context_from_ancestors`` reuses the most
      recent DRAFT ancestor's ``input_messages`` as the seed for
      tool-loop follow-ups, so prepending again would stack a new
      copy each iteration. Observed 2026-04-25 on the v7 qwen36 run:
      a worker draft 4 tool-loop iterations deep had 4 copies of
      the runtime note as system messages [0..3]. Dedup keeps it at
      exactly one copy per node's saved context.

    The note is fully pre-rendered by the caller (ChatFlowEngine
    combines static user text + dynamic OS / shell / cwd hints into
    one string). The engine's only job here is the tool-gated
    prepend.

    Cache-friendly: the note carries no per-call dynamic content (no
    timestamps, no per-turn ids), so identical notes across calls
    share the same prefix and KV cache stays warm.
    """
    if not tool_defs:
        return messages
    text = (note or "").strip()
    if not text:
        return messages
    if (
        messages
        and isinstance(messages[0], SystemMessage)
        and messages[0].content == text
    ):
        # Already-prepended (carried in via a saved ancestor's
        # ``input_messages``); leave the seed alone instead of
        # cascading into a stack of identical system messages.
        return messages
    return [SystemMessage(content=text), *messages]


def _wire_to_provider(wires: list[WireMessage]) -> list[Message]:
    """Translate the schema-side WireMessage list into the provider-facing
    Message union. Preserves order (KV cache contract, ADR-013)."""
    out: list[Message] = []
    for w in wires:
        if w.role == "system":
            out.append(SystemMessage(content=w.content))
        elif w.role == "user":
            out.append(UserMessage(content=w.content))
        elif w.role == "assistant":
            out.append(
                AssistantMessage(
                    content=w.content,
                    tool_uses=[
                        ProviderToolUse(id=tu.id, name=tu.name, arguments=dict(tu.arguments))
                        for tu in w.tool_uses
                    ],
                )
            )
        elif w.role == "tool":
            # M3 does not execute tools, but the mapping exists for
            # symmetry — M6 will use it.
            from agentloom.providers.types import ToolMessage

            out.append(ToolMessage(tool_use_id=w.tool_use_id or "", content=w.content))
        else:  # pragma: no cover
            raise ValueError(f"unknown wire role {w.role}")
    return out


def _provider_to_wire(messages: list[Message]) -> list[WireMessage]:
    out: list[WireMessage] = []
    for m in messages:
        extras = dict(m.extras) if m.extras else {}
        if isinstance(m, SystemMessage):
            out.append(WireMessage(role="system", content=m.content, extras=extras))
        elif isinstance(m, UserMessage):
            out.append(WireMessage(role="user", content=m.content, extras=extras))
        elif isinstance(m, AssistantMessage):
            out.append(
                WireMessage(
                    role="assistant",
                    content=m.content or "",
                    tool_uses=[
                        SchemaToolUse(id=tu.id, name=tu.name, arguments=dict(tu.arguments))
                        for tu in m.tool_uses
                    ],
                    extras=extras,
                )
            )
        else:
            # ToolMessage
            out.append(
                WireMessage(
                    role="tool",
                    content=m.content,
                    tool_use_id=getattr(m, "tool_use_id", None),
                    extras=extras,
                )
            )
    return out


def _build_context_from_ancestors(workflow: WorkFlow, node: WorkFlowNode) -> list[Message]:
    """Flat message list — see :func:`_build_tagged_context_from_ancestors`
    for the full walk logic. This helper strips source-node annotations
    since providers only want ``Message`` objects."""
    return [m for _, m in _build_tagged_context_from_ancestors(workflow, node)]


def _build_tagged_context_from_ancestors(
    workflow: WorkFlow, node: WorkFlowNode
) -> list[tuple[str | None, Message]]:
    """Topologically walk ancestors and reconstruct the OpenAI-style
    message list, tagging each entry with the WorkNode id it came from.

    The tag is what the compact worker cites next to summary segments so
    readers can pull full content via ``get_node_context``; for callers
    that only want the flat message list, :func:`_build_context_from_ancestors`
    strips the tags.

    Seed selection:
    - A settled COMPACT ancestor acts as a hard cutoff: everything
      before (and including) it is replaced by a single user message
      holding the summary plus the snapshot's preserved recent turns.
      The synthetic preamble is tagged with the compact node's id;
      preserved tail messages are tagged ``None`` because their original
      sources were folded into the summary.
    - Otherwise, the **most recent** llm_call ancestor's
      ``input_messages`` provides the seed (tagged with that ancestor),
      and that ancestor's ``output_message`` follows immediately. This
      anchors tool-loop follow-ups to the same system prompt as the
      call they're continuing.

    After the seed, remaining ancestors layer on in topological order:
    - llm_call contributes its ``output_message`` (assistant turn,
      possibly with tool_uses) tagged with the node.
    - tool_call contributes a ``tool`` message tagged with the tool_call
      node itself.
    """
    ancestors = workflow.ancestors(node.id)

    compact_cutoff_idx: int | None = None
    for i, aid in enumerate(ancestors):
        a = workflow.get(aid)
        if (
            a.step_kind == StepKind.COMPRESS
            and a.compact_snapshot is not None
            and a.compact_snapshot.summary
        ):
            compact_cutoff_idx = i

    tagged: list[tuple[str | None, Message]] = []
    start_idx = 0

    if compact_cutoff_idx is not None:
        cutoff_node = workflow.get(ancestors[compact_cutoff_idx])
        snap = cutoff_node.compact_snapshot
        assert snap is not None
        tagged.append(
            (
                cutoff_node.id,
                UserMessage(
                    content=(
                        "[Prior conversation — summarized to save context]\n\n"
                        f"{snap.summary}"
                    )
                ),
            )
        )
        for m in _wire_to_provider(snap.preserved_messages):
            tagged.append((None, m))
        start_idx = compact_cutoff_idx + 1
    else:
        seed_idx: int | None = None
        for i, aid in enumerate(ancestors):
            a = workflow.get(aid)
            if a.step_kind == StepKind.DRAFT and a.input_messages:
                seed_idx = i
        if seed_idx is not None:
            seed_owner = workflow.get(ancestors[seed_idx])
            assert seed_owner.input_messages is not None
            for m in _wire_to_provider(seed_owner.input_messages):
                tagged.append((seed_owner.id, m))
            if seed_owner.output_message is not None:
                for m in _wire_to_provider([seed_owner.output_message]):
                    tagged.append((seed_owner.id, m))
            start_idx = seed_idx + 1

    for aid in ancestors[start_idx:]:
        a = workflow.get(aid)
        if a.step_kind == StepKind.DRAFT:
            if a.output_message is not None:
                for m in _wire_to_provider([a.output_message]):
                    tagged.append((a.id, m))
        elif a.step_kind == StepKind.TOOL_CALL:
            if a.tool_result is not None and a.source_tool_use_id is not None:
                tagged.append(
                    (
                        a.id,
                        ToolMessage(
                            tool_use_id=a.source_tool_use_id,
                            content=_maybe_truncate_tool_result(
                                a.tool_result.content, a.id, a.tool_name
                            ),
                        ),
                    )
                )
    return tagged
