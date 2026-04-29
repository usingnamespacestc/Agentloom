"""Demo: long Chinese auto_plan conversation showcasing as many
features as possible. Designed for canvas-side viewing — the
chatflow is preserved in the DB after the run, the cf_id is
printed at start so you can pull it up before the run finishes.

Feature surface this demo touches (canvas-visible):

ChatFlow layer
- 11+ turns in auto_plan (full cognitive pipeline every turn)
- fork from a non-leaf ChatNode (mid-chain branch)
- manual compact with custom instruction + preserve_recent_turns
- drill-down via ``get_node_context`` through the compact
- (probable) Tier-1 auto-compact during the long final synthesis
- Chinese compact_instruction + Chinese fixtures (zh-CN)

WorkFlow layer (every turn fires this, since auto_plan)
- judge_pre + trio extraction
- judge_pre cognitive recon DAG (when judge_pre wants to verify
  something it can't see in transcript)
- planner ``atomic step_kind=draft`` (most reasoning turns)
- planner ``atomic step_kind=tool_call`` (the Glob / Read turns
  trigger the post-70fec62 path: planner picks the tool by name +
  args; engine spawns a real TOOL_CALL WorkNode directly)
- planner ``decompose`` (turn 3 explicitly asks for 3 parallel
  subtasks)
- planner_judge during verdict
- worker_judge debate / capability_request / etc.
- judge_post universal exit gate
- judge_post recon DAG (post-side verification when needed)
- planner-grounding fuse + tool-loop budget = unlimited (the
  2026-04-29 default flip — long tool-using turns no longer
  capped at 12 iterations)

Tooling
- Glob + Read (filesystem read tools, against Agentloom's own
  ``frontend/src``)
- get_node_context (drill-down)

MemoryBoard
- chat_brief auto-spawn (LLM-generated brief for every ChatNode)
- node_brief auto-spawn (per-WorkNode brief)
- produced_tags / consumed_tags collection
- BoardReader → judge_post layer-notes

Wall-clock: 45-90 min on doubao free tier. Each auto_plan turn
fires 4-7 LLM calls; turns that decompose or tool-loop fire more.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "smoke"))

# Force-keep so the chatflow survives for canvas browsing. This
# overrides _common.delete_chatflow's no-op behavior.
os.environ["AGENTLOOM_SMOKE_KEEP"] = "1"

from _common import (  # noqa: E402
    BACKEND_URL,
    backend_client,
    create_chatflow,
    get_chatflow,
    smoke,
    submit_turn,
)


# Canvas URL templating: BACKEND is :8000, frontend dev is :5173 by
# convention from scripts/dev.sh.
def _canvas_url(cf_id: str) -> str:
    return f"{BACKEND_URL.replace('8000', '5173')}/chatflows/{cf_id}"


# -------- Phase 1: plant facts + light reasoning chain (5 turns) --------
PHASE1_TURNS = [
    "我正在做一个个人理财 dashboard 项目。技术栈：前端用 React + TypeScript + "
    "Tailwind，数据库用 PostgreSQL，部署到 Vercel。请把这些技术栈约束都"
    "记下来——后面我会回来逐字问你。",
    "请先列出这个 dashboard 应该包含哪些核心功能模块，按重要性从高到低排序，"
    "每条用一句话说明。",
    "在你刚列出的功能模块里，挑出最重要的 3 个。**对这 3 个分别给出主要数据"
    "模型**——字段名、类型、是否可空、外键关系。请把每个模块当作一个独立的"
    "子任务并行处理，最后汇总。",
    "刚才那 3 张表，在 PostgreSQL 里建表的话分别该建哪些索引？为什么？",
    "针对这 3 个里你认为最重要的那一个，给我一棵 React 组件树（用纯文字"
    "描述，包括 prop 流向）。",
]

# -------- Phase 2: tool-call heavy turns continuing main chain --------
PHASE2_TURNS = [
    "我突然好奇 Agentloom 自己是怎么组织前端代码的。请用 Glob 工具搜一下 "
    "/home/usingnamespacestc/Agentloom/frontend/src 下所有 .tsx 文件的"
    "路径，给我看到主要目录结构就行。",
    "结合你刚刚看到的 Agentloom frontend 目录组织方式，给我的理财 dashboard "
    "推荐一种组件目录组织风格，并说明为什么适合（结合规模、协作、可测性几个"
    "角度）。",
    "请用 Read 工具看 /home/usingnamespacestc/Agentloom/frontend/src/canvas/"
    "ChatFlowCanvas.tsx 的前 80 行，告诉我它的整体结构和主要职责。",
    "把我们前面讨论的所有内容串起来：前端组件 → API → PostgreSQL，整体的"
    "数据流是怎么走的？画一张文字版的流程图。",
]

# -------- Phase 3: compact, drill, final synthesis --------
COMPACT_INSTRUCTION = (
    "概括上面的对话内容。**特别提示**：用户最早第 1 轮里给出了完整的技术栈"
    "约束（含一个具体的 UI 框架名）——务必在 summary 里保留这条原始信息。"
    "对每个被压缩的节点，请保留它的节点 id 引用，让后续可以用 "
    "get_node_context(node_id=<id>) 取回原文。"
)

PHASE3_DRILL = (
    "回到最初：第 1 轮里我说的「前端 UI 框架」，原话用的是哪个词？"
    "请精确给出那个词，包括大小写。"
)

PHASE3_FINAL = (
    "基于我们之前讨论过的所有内容（核心功能模块、3 个数据模型、索引策略、"
    "React 组件树、Agentloom 前端组件组织借鉴、整体数据流），请给出一份"
    "**最终的项目实施计划**：分阶段交付物、每阶段大致工程量估算、"
    "以及最关键的 2-3 个风险点。"
)

# -------- Phase 4: fork from mid-chain, AFTER main chain is done --------
PHASE4_FORK = (
    "换个完全不同的话题：简单介绍一下 PostgreSQL 的 4 种事务隔离级别"
    "（read uncommitted / read committed / repeatable read / serializable），"
    "各自能防住哪类读异常。"
)


async def main() -> None:
    async with smoke("demo · 长对话 auto_plan 全 feature 中文") as report:
        async with backend_client() as client:
            cf_id = await create_chatflow(
                client,
                title="demo · 个人理财 dashboard (auto_plan zh)",
                execution_mode="auto_plan",
                cognitive_react_enabled=True,
                extra_patch={
                    # Leave Tier-1 (workflow-layer) auto-compact armed
                    # so the final synthesis turn can demo it. Disable
                    # Tier-2 (chatnode-layer) so the planted-fact
                    # trigger doesn't fire mid-phase-1.
                    "compact_trigger_pct": 0.85,
                    "chatnode_compact_trigger_pct": None,
                },
            )
            print(
                f"\n===> chatflow id: {cf_id}\n"
                f"===> open in canvas: {_canvas_url(cf_id)}\n",
                flush=True,
            )
            report.add(f"chatflow created: {cf_id}", True)

            # Phase 1 — direct chain through 5 reasoning turns.
            report.section(f"phase 1 — 中文 {len(PHASE1_TURNS)} 轮直链 (auto_plan)")
            phase1: list[dict] = []
            for i, prompt in enumerate(PHASE1_TURNS, start=1):
                t = await submit_turn(client, cf_id, prompt)
                phase1.append(t)
                report.add(
                    f"turn {i:>2} succeeded",
                    t.get("status") == "succeeded",
                    f"status={t.get('status')}",
                )
            t5_id = phase1[-1]["node_id"]

            # Phase 2 — tool-call heavy turns extending the main chain.
            # submit_turn picks the latest leaf, which is t5 (no fork
            # exists yet in this script's ordering).
            report.section(
                f"phase 2 — tool-call 重度段（{len(PHASE2_TURNS)} 轮）"
            )
            phase2: list[dict] = []
            for i, prompt in enumerate(PHASE2_TURNS, start=1):
                t = await submit_turn(client, cf_id, prompt)
                phase2.append(t)
                report.add(
                    f"phase2 turn {i} succeeded",
                    t.get("status") == "succeeded",
                    f"status={t.get('status')}",
                )
            last_main = phase2[-1]

            # Phase 3 — compact main chain at the latest node, then
            # drill back to t1's planted fact.
            report.section("phase 3a — 主链末端手动 compact")
            rcompact = await client.post(
                f"/api/chatflows/{cf_id}/nodes/"
                f"{last_main['node_id']}/compact",
                json={
                    "preserve_recent_turns": 2,
                    "compact_instruction": COMPACT_INSTRUCTION,
                },
            )
            report.add(
                "POST /compact returns 200",
                rcompact.status_code == 200,
                f"status={rcompact.status_code}",
            )
            cf_state = await get_chatflow(client, cf_id)
            compacts = [
                n for n in (cf_state.get("nodes") or {}).values()
                if n.get("compact_snapshot") is not None
            ]
            report.add(
                "compact ChatNode created",
                len(compacts) >= 1,
                f"count={len(compacts)}",
            )

            report.section("phase 3b — drill-down through compact")
            drill = await submit_turn(client, cf_id, PHASE3_DRILL)
            report.add(
                "drill turn returns 200",
                drill.get("status") == "succeeded",
                f"status={drill.get('status')}",
            )
            ans = drill.get("agent_response") or ""
            report.add(
                "drill recovers 'Tailwind' (case-insensitive)",
                "tailwind" in ans.lower(),
                f"text={ans[:200]!r}",
            )

            report.section("phase 3c — final synthesis (长 ancestor walk)")
            final = await submit_turn(client, cf_id, PHASE3_FINAL)
            report.add(
                "final turn returns 200",
                final.get("status") == "succeeded",
                f"status={final.get('status')}",
            )

            # Phase 4 — fork from t5 with the postgres isolation prompt.
            # Done last so submit_turn's "latest leaf" semantics on the
            # earlier phases don't have to worry about the fork.
            report.section("phase 4 — fork from t5 (后置)")
            fork = await submit_turn(
                client, cf_id, PHASE4_FORK, parent_id=t5_id
            )
            report.add(
                "fork from non-leaf t5",
                fork.get("status") == "succeeded",
                f"status={fork.get('status')}",
            )

            # Final summary banner — repeated so the cf_id sits at the
            # bottom of the log too, not just the top.
            print(
                f"\n===> chatflow PRESERVED in DB: {cf_id}\n"
                f"===> open in canvas: {_canvas_url(cf_id)}\n",
                flush=True,
            )


if __name__ == "__main__":
    asyncio.run(main())
