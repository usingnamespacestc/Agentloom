"""Render a :class:`JudgeVerdict` into a human-readable prompt that the
ChatFlow layer surfaces as the agent's turn when a WorkFlow halts for
user clarification.

All user-visible dialogue lives at the ChatFlow layer; the WorkFlow
never talks to the user directly. When a judge pass decides the
WorkFlow cannot proceed (judge_pre says non-OK, or judge_post says
retry/fail), the engine sets ``workflow.pending_user_prompt`` to one
of the strings produced here, and the ChatFlow engine uses it verbatim
as the new ChatNode's ``agent_response``.
"""

from __future__ import annotations

from agentloom.schemas.common import JudgeVerdict


def _lang() -> str:
    """Return the workspace's currently-configured prompt language
    (``"zh-CN"``, ``"en-US"``, …). Falls back to ``"en-US"`` if the
    tenancy runtime isn't initialised (tests, standalone usage)."""
    try:
        from agentloom import tenancy_runtime
        from agentloom.db.models.tenancy import DEFAULT_WORKSPACE_ID

        return tenancy_runtime.get_settings(DEFAULT_WORKSPACE_ID).language
    except Exception:
        return "en-US"


def _is_zh(lang: str | None = None) -> bool:
    return (lang or _lang()).lower().startswith("zh")


def judge_pre_needs_user_input(verdict: JudgeVerdict) -> bool:
    """Does this judge_pre verdict require bouncing back to the user?

    Only ``infeasible`` or non-empty ``missing_inputs`` block the run.
    ``risky`` is defined (judge_pre.yaml) as "proceed is possible but
    specific assumptions must hold" — the engine threads those
    assumptions to the planner as handoff notes rather than halting.
    """
    if verdict.feasibility == "infeasible":
        return True
    if verdict.missing_inputs:
        return True
    return False


def judge_post_needs_user_input(verdict: JudgeVerdict) -> bool:
    """judge_post halts back to ChatFlow on anything that isn't
    ``accept`` — retry/fail both require user decision on next steps."""
    return verdict.post_verdict != "accept"


def format_judge_pre_prompt(verdict: JudgeVerdict) -> str:
    """Compose a clarifying question for the user from a judge_pre
    verdict. The text is intentionally conversational — it will be
    shown as the assistant's side of a ChatNode."""
    zh = _is_zh()
    lines: list[str] = []

    if verdict.feasibility == "infeasible":
        lines.append(
            "开始之前，我需要先指出：这个任务按现在的形式很可能不可行。"
            if zh
            else "Before I start, I need to flag this task as likely infeasible in its current form."
        )
    elif verdict.feasibility == "risky":
        lines.append(
            "开始之前，我想先和你确认几件事——这个任务看起来有风险。"
            if zh
            else "Before I start, I want to check a few things with you — this task looks risky."
        )
    else:
        lines.append(
            "开始之前，我想确认一下手上的信息是否足够。"
            if zh
            else "Before I start, I want to make sure I have what I need."
        )

    if verdict.blockers:
        lines.append("")
        lines.append("**我看到的阻塞点：**" if zh else "**Blockers I see:**")
        for b in verdict.blockers:
            lines.append(f"- {b}")

    if verdict.missing_inputs:
        lines.append("")
        lines.append("**我还需要：**" if zh else "**I still need:**")
        for m in verdict.missing_inputs:
            lines.append(f"- {m}")

    lines.append("")
    lines.append(
        "能否澄清一下，或者要我直接继续？"
        if zh
        else "Could you clarify, or should I proceed anyway?"
    )
    return "\n".join(lines)


def format_revise_budget_halt_prompt(
    revise_count: int,
    budget: int,
    latest_verdict: JudgeVerdict | None,
) -> str:
    """Compose a check-in for the user when ``judge_during`` has returned
    ``revise`` ``budget + 1`` times in this WorkFlow run (§5.3 FR-PL-7).

    The engine halts auto-mode at this point rather than loop forever;
    the user decides whether to let it keep going, tighten the plan, or
    bail. ``latest_verdict`` is the one that pushed the counter over —
    used to surface the specific critique the judge flagged last.
    """
    zh = _is_zh()
    if zh:
        lines: list[str] = [
            f"我已经触达自动模式的返工上限（{revise_count}/{budget}）——"
            "评审反复挑刺，想先听听你的判断再决定怎么走。",
        ]
    else:
        lines = [
            f"I've hit the auto-mode revise limit ({revise_count}/{budget}) —"
            " the critic keeps flagging this run, so I'd like your take before"
            " I keep going.",
        ]
    if latest_verdict is not None and latest_verdict.critiques:
        lines.append("")
        lines.append("**最新一轮评审意见：**" if zh else "**Latest critique:**")
        for c in latest_verdict.critiques:
            piece = f"- **{c.severity}** — {c.issue}"
            if c.evidence:
                piece += (
                    f"（依据：{c.evidence}）"
                    if zh
                    else f" (evidence: {c.evidence})"
                )
            lines.append(piece)
    lines.append("")
    lines.append(
        "要继续推进、调整计划，还是直接停下？"
        if zh
        else "Should I push through, adjust the plan, or stop?"
    )
    return "\n".join(lines)


def format_ground_ratio_halt_prompt(
    *,
    leaves: int,
    tools: int,
    min_ratio: float,
) -> str:
    """Compose a check-in when the planner-grounding fuse trips.

    Fires when the ratio of completed ``tool_call`` leaves to all
    completed non-``sub_agent_delegation`` leaves drops below
    ``min_ratio`` after the grace threshold. Signals the planner is
    decomposing/judging without ever reaching a real action — the
    2026-04-17 incident hit 2 tool_calls out of 392 nodes. See §5.4.
    """
    ratio = (tools / leaves) if leaves > 0 else 0.0
    if _is_zh():
        return (
            f"我好像在空转——到目前为止 {leaves} 步里只有 {tools} 次工具调用"
            f"（实际 {ratio:.1%}，阈值 {min_ratio:.1%}）。计划器可能一直在"
            "分解和审核，却始终没有真正执行动作。要我继续推进、简化任务，"
            "还是先停下？"
        )
    return (
        f"I seem to be spinning — only {tools} tool call(s) out of "
        f"{leaves} completed steps so far (ratio {ratio:.1%}, threshold "
        f"{min_ratio:.1%}). The planner may be decomposing and judging "
        "without ever landing on a real action. Should I keep pushing, "
        "simplify the task, or stop?"
    )


def format_judge_post_prompt(verdict: JudgeVerdict) -> str:
    """Compose a check-in for the user from a judge_post verdict.
    Used when the post pass returns retry/fail and the agent needs the
    user to decide next steps.

    Option B (judge_post is the universal exit gate): the judge itself
    writes ``user_message`` in its own voice — when present we return
    it verbatim. The structured fallback below only fires for verdicts
    whose ``user_message`` is missing (legacy callers, or a model that
    didn't fill the field)."""
    if verdict.user_message:
        return verdict.user_message

    zh = _is_zh()
    lines: list[str] = []

    if verdict.post_verdict == "fail":
        lines.append(
            "我把计划跑完了，但结果达不到预期。"
            if zh
            else "I ran the plan but the outcome does not meet the expected result."
        )
    elif verdict.post_verdict == "retry":
        lines.append(
            "计划跑完了，不过我想重试一次——结果不太对。"
            if zh
            else "I finished the plan but I'd like to retry — the result is not quite right."
        )
    else:  # defensive — should not be called on accept
        lines.append("计划已完成。" if zh else "The plan completed.")

    if verdict.issues:
        lines.append("")
        lines.append("**我观察到的问题：**" if zh else "**What I observed:**")
        for issue in verdict.issues:
            if zh:
                piece = (
                    f"- **{issue.location}** — 期望 `{issue.expected}`，"
                    f"实际 `{issue.actual}`"
                )
                if issue.reproduction:
                    piece += f"（复现：{issue.reproduction}）"
            else:
                piece = (
                    f"- **{issue.location}** — expected `{issue.expected}`, "
                    f"got `{issue.actual}`"
                )
                if issue.reproduction:
                    piece += f" (repro: {issue.reproduction})"
            lines.append(piece)

    lines.append("")
    lines.append(
        "你想怎么往下走？"
        if zh
        else "How would you like to proceed?"
    )
    return "\n".join(lines)
