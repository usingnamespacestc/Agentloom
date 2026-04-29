"""Combo: 中文 12 轮 full chatflow life-cycle smoke (doubao + auto_plan).

Mirrors ``combo_full_pipeline.py`` but with:
- All user turns in Chinese, exercising the engine on non-English
  conversation (fixtures + provider param paths handle UTF-8
  symmetrically with English on doubao, but the prior smoke didn't
  pin that — this one does).
- ``auto_plan`` as the chatflow's default execution mode for **every
  turn**, not just the final drill. Pre-fix the English variant
  saved doubao quota by running phase 1+2 in ``native_react`` and
  flipping to ``auto_plan`` only for the drill turn; this run
  exercises the full cognitive pipeline (judge_pre → planner →
  planner_judge → worker / atomic-tool_call → judge_post) on every
  one of the 12 turns, so any auto_plan-only regression that the
  English combo would mask surfaces here.
- 10+ phase-1 turns to stress the compact preserve_recent_turns +
  ancestor-walk pathway with a longer chain than the 3-turn shape
  the English variant uses.

Wall-clock budget: roughly 30-60 min on the volcengine doubao free
tier. Each auto_plan turn fires 4-6 LLM calls (judge_pre +
planner + planner_judge + worker/tool + post_judge ± recon DAG),
so 12 turns is markedly slower than a native_react chain. The
smoke client's per-turn timeout is already 1800s (30 min) — sized
for the slowest realistic single turn — so individual turns won't
time out, but the operator should expect to wait.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _common import (  # noqa: E402
    all_worknodes,
    backend_client,
    create_chatflow,
    delete_chatflow,
    get_chatflow,
    smoke,
    submit_turn,
)


PHASE1_TURNS = [
    # T1 plants the fact we'll drill back to in phase 4. The exact
    # word ``八紫色`` is fictional (a transliteration of Pratchett's
    # "octarine") so the only way to recover it is via get_node_context
    # over the compacted ancestor — recall by recall from the model
    # alone won't work.
    "我最喜欢的颜色是「八紫色」。这是一种虚构的颜色，请你记住这个原词，"
    "包括引号。简要确认你已经记住即可。",
    "给我讲一个关于猫的冷知识。",
    "再讲一个关于大象的。",
    "讲一个关于章鱼的有趣事实。",
    "章鱼对颜色的感知方式跟人类一样吗？",
    "海豚和鲸鱼的睡眠机制是不是有别于陆地哺乳动物？简单说说就好。",
    "鸟类会做梦吗？大概聊一下证据。",
    "蝙蝠靠回声定位，那它们的视觉是不是退化了？",
    "再随便讲一个关于深海生物的小故事。",
    "好的，今天聊得差不多了，给我一句话总结一下我们刚才讨论过的动物主题。",
]


async def main() -> None:
    async with smoke("combo full pipeline (中文 12 轮)") as report:
        async with backend_client() as client:
            cf_id = await create_chatflow(
                client,
                title="smoke combo zh (auto_plan)",
                # auto_plan from the start — every turn fires the
                # full cognitive pipeline. See module docstring for
                # the wall-clock implications.
                execution_mode="auto_plan",
                cognitive_react_enabled=True,
                extra_patch={
                    "compact_trigger_pct": None,
                    "chatnode_compact_trigger_pct": None,
                },
            )
            try:
                # Phase 1: 10 中文轮 in auto_plan，把"八紫色"这个事实埋在第 1 轮。
                report.section(
                    f"phase 1 — 中文 {len(PHASE1_TURNS)} 轮直链 (auto_plan)"
                )
                phase1: list[dict] = []
                for i, prompt in enumerate(PHASE1_TURNS, start=1):
                    t = await submit_turn(client, cf_id, prompt)
                    phase1.append(t)
                    report.add(
                        f"turn {i:>2} succeeded",
                        t.get("status") == "succeeded",
                        f"status={t.get('status')}",
                    )
                last_main = phase1[-1]

                # Phase 2: 在中段（第 5 轮）开 fork，验证 fork-from-non-leaf
                # 在中文上下文里也能正常工作。
                report.section("phase 2 — 中段 fork（在 t5 上分叉）")
                fork = await submit_turn(
                    client,
                    cf_id,
                    "换个话题：12 乘以 7 等于多少？",
                    parent_id=phase1[4]["node_id"],
                )
                report.add(
                    "fork from non-leaf t5",
                    fork.get("status") == "succeeded",
                    f"status={fork.get('status')}",
                )

                # Phase 3: 在主链末端 (t10) 触发手动 compact，preserve 最近
                # 1 轮——这样早期"八紫色"那一轮被压缩进 summary，drill
                # 必须靠 get_node_context 才能取回原词。
                report.section("phase 3 — 主链末端手动 compact")
                rcompact = await client.post(
                    f"/api/chatflows/{cf_id}/nodes/"
                    f"{last_main['node_id']}/compact",
                    json={
                        "preserve_recent_turns": 1,
                        "compact_instruction": (
                            "概括这段对话。用户在最早的一轮里告诉了你"
                            "他最喜欢的颜色（一个虚构词）。请保留所有"
                            "节点的引用 id 以便后续 get_node_context 取回原文。"
                        ),
                    },
                )
                report.add(
                    "POST /compact returns 200",
                    rcompact.status_code == 200,
                    f"status={rcompact.status_code}",
                )

                cf_state = await get_chatflow(client, cf_id)
                compacts = [
                    n
                    for n in (cf_state.get("nodes") or {}).values()
                    if n.get("compact_snapshot") is not None
                ]
                report.add(
                    "compact ChatNode created",
                    len(compacts) >= 1,
                    f"count={len(compacts)}",
                )

                # Phase 4: 仍在 auto_plan，要求精确取回"八紫色"原词。
                # 与英文变体不同——这条路径在前 11 轮就已经在 auto_plan
                # 下跑完，drill 只是最后一轮，没有模式切换的扰动。
                report.section(
                    "phase 4 — drill-down through compact (auto_plan)"
                )
                drill = await submit_turn(
                    client,
                    cf_id,
                    "我最早告诉你的「我最喜欢的颜色」原话用了哪个词？"
                    "请精确给出那个词，包括是否带引号。",
                )
                report.add(
                    "drill turn returns 200",
                    drill.get("status") == "succeeded",
                    f"status={drill.get('status')}",
                )
                ans = drill.get("agent_response") or ""
                report.add(
                    "answer recovers exact word '八紫色'",
                    "八紫色" in ans,
                    f"text={ans[:200]!r}",
                )

                cf_final = await get_chatflow(client, cf_id)
                drill_wn = all_worknodes(
                    cf_final, chat_node_id=drill["node_id"]
                )
                judge_pre_count = sum(
                    1 for w in drill_wn if w.get("role") == "pre_judge"
                )
                planner_count = sum(
                    1 for w in drill_wn if w.get("role") == "plan"
                )
                worker_count = sum(
                    1 for w in drill_wn if w.get("role") == "worker"
                )
                judge_post_count = sum(
                    1 for w in drill_wn if w.get("role") == "post_judge"
                )
                tool_calls = [
                    w
                    for w in drill_wn
                    if w.get("step_kind") == "tool_call"
                ]
                tool_names = sorted(
                    {w.get("tool_name") or "" for w in tool_calls}
                )
                report.add(
                    "auto_plan pipeline ran (judge_pre + planner + worker/tool_call + judge_post)",
                    judge_pre_count >= 1
                    and planner_count >= 1
                    # Either a draft worker or a direct tool_call
                    # (atomic step_kind=tool_call path, see commit
                    # 70fec62) is acceptable — both produce a usable
                    # answer node downstream of the planner.
                    and (worker_count >= 1 or tool_calls)
                    and judge_post_count >= 1,
                    f"pre={judge_pre_count} plan={planner_count} "
                    f"worker={worker_count} tools={len(tool_calls)} "
                    f"post={judge_post_count}",
                )
                report.add(
                    "recon recursion fuse caps judge_pre at ≤2",
                    judge_pre_count <= 2,
                    f"count={judge_pre_count}",
                )
                report.add(
                    "drill-down invoked get_node_context",
                    "get_node_context" in tool_names,
                    f"tools={tool_names}",
                )
                ans_lower = ans.lower()
                report.add(
                    "user-facing reply has no engine-speak halt template",
                    not any(
                        s in ans_lower
                        for s in (
                            "internal error",
                            "system error",
                            "system failure",
                            "execution flow malfunctioned",
                        )
                    )
                    and not any(
                        s in ans
                        for s in (
                            "内部错误",
                            "系统错误",
                            "执行流故障",
                        )
                    ),
                    f"text={ans[:120]!r}",
                )
            finally:
                await delete_chatflow(client, cf_id)


if __name__ == "__main__":
    asyncio.run(main())
