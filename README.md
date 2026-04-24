[English](./README.en.md) | **中文**

# Agentloom

> **⚠️ 快速迭代中。** Agentloom 仍在高速开发——里程碑按天推进，API
> 随时在变，边角粗糙在所难免。你看到的是一个活跃原型的当前快照，而不是成品。
> 页尾链接的开发日志才是"发生了什么、为什么这样改"的权威记录。

**Agentloom 是一个可视化的 Agent 工作流平台：每一段对话都是可分支、可 fork、可 merge
的 DAG，每一步 agent 执行都可以被拆开查看。** 这里既不是线性聊天记录，也不是预先
手工连好的流水线——**对话本身就是一张图**，你可以 fork、merge、compact、回放；内置
的动态 planner 在执行过程中把目标拆解成子任务再递归下去。

![ChatFlow canvas: 四条"资料讲解"对话分支在同一个 LCA 下展开，两条深入分支最终
合流到一个 merge ChatNode，右栏同步显示当前选中节点的 ConversationView。](docs/images/01-chatflow-canvas.png)

---

## 为什么有意思

常见的 "agent" UI 通常会退化成两种形态：一种是线性聊天框，一种是预先写死的静态
工作流。Agentloom 想把两者同时做掉，还加上了第三个维度——**对话即工作流**，而这
个工作流是用户可以直接编辑的一等公民 DAG。

具体来说：

- **多线程对话。** 任何一个 ChatNode 都可以被 fork——新分支继承 fork 点之前的
  历史，然后各自独立演进。多个分支并行存在，互不干扰；系统里没有所谓的"当前
  会话"——你有多少个想法就有多少个活跃分支。

- **合流（Merge）。** 两条探索了不同策略的分支可以被折回成一个节点，这个节点的
  回复会综合两边的结论。后续对话从合流后的结论继续。fork 加 merge 让对话树真正
  变成 DAG，而不只是一棵树。

- **压缩（Compact）。** 超长对话会被归纳成一个紧凑的 ChatNode：既可以在 ChatFlow
  层手动触发（显式、用户可见），也可以在 WorkFlow 层自动触发（隐式，在 llm_call
  即将撑破上下文时启动）。两层压缩都 dogfood 同一个 `compact.yaml` 模板——压缩本
  身就是一个可复用的 workflow，而不是写死在 engine 里的动作。

  ![Compact ChatNode：前三轮背景资料被自动归纳成一个浅色的 compact 节点插在对话链中间，
  summary 直接引用源节点 id 作为 "citation"；紧随其后的真实问题在
  "summary + 保留 tail" 之上继续推理。](docs/images/02-chatflow-compact.png)

- **动态 planner + 嵌套 WorkFlow。** 每个 ChatNode 内部都有一个 WorkFlow DAG：
  模型调用、工具调用、子 agent 委派、judge 判定。面对复杂目标，planner 会把任务
  分解成一张 WorkNode 的 DAG，能并行就并行，并在每个阶段（pre / during / post）
  让 judge 审阅产出。子 agent 委派会开出一个嵌套的 WorkFlow，它的事件流会往上
  转发到父 WorkFlow。

  ![WorkFlow 内部视图（S1 retry 流）：auto_plan 模式下 20 个 WorkNode 展开成
  judge_pre → plan → plan_judge → 多个并行 worker/worker_judge → 4-parent
  扇入的 post_judge → retry cycle 再跑一轮的 DAG。每个 WorkNode 都能看到
  自己的输入、输出、状态、耗时；右下角的 WorkBoard 浮窗列出全部 brief。
  ](docs/images/04-workflow-canvas.png)

- **Plan / Execute 分离。** 每个节点都有两态：虚线（已规划，可编辑）→ 实线（已
  执行，已冻结）。你可以在执行前修改计划，执行后查看现场；想重来就开分支——原来
  的尝试不会被覆盖。

- **完整可观测。** SSE 事件流把 engine 的每一次状态变化都推到 canvas 上。每个
  节点的 token 用量、延迟、judge 判定、retry 状态都能看到。Node ID、模型元数据、
  工具调用参数都对用户可见，方便理解 agent "为什么这么做"。

---

## 整体架构 · 一个请求是如何被拆解的

```mermaid
flowchart TB
  subgraph CF["<b>ChatFlow</b> · 对话即 DAG（用户看到的外层）"]
    direction LR
    U((User)) --> CN1[ChatNode A<br/>简单问答]
    CN1 --> CN2[ChatNode B<br/>复杂任务<br/>auto_plan]
    CN2 -. fork .-> CN2B[ChatNode B']
    CN2 --> MG([merge ChatNode])
    CN2B --> MG
    MG --> CN3[ChatNode C]
  end

  CN2 -. ⤢ 钻入 .-> WF

  subgraph WF["<b>内层 WorkFlow</b> · 动态计划 + 多级审核"]
    direction TB
    JPRE{{"judge_pre<br/>① 前置审核<br/>任务是否明确?"}} --> PL["planner<br/>② 分解为 worker DAG"]
    PL --> PJ{{"plan_judge<br/>③ 计划审核<br/>分解是否合理?"}}
    PJ --> W1["worker 1<br/>tool_call"]
    PJ --> W2["worker 2<br/><b>sub_agent_delegation</b>"]
    PJ --> W3["worker 3<br/>tool_call"]
    W1 --> WJ1{{"worker_judge"}}
    W2 --> WJ2{{"worker_judge"}}
    W3 --> WJ3{{"worker_judge"}}
    WJ1 --> JPOST{{"judge_post<br/>④ 产出审核<br/>目标是否达成?"}}
    WJ2 --> JPOST
    WJ3 --> JPOST
    JPOST -- pass --> OUT([agent_response])
    JPOST -. fail + redo_targets .-> PL
  end

  W2 -. <b>递归</b>展开 .-> SUB

  subgraph SUB["<b>嵌套 WorkFlow</b> · 子 agent 委派（深度可任意叠加）"]
    direction TB
    SJP{{judge_pre}} --> SPL[planner]
    SPL --> SW1[worker · tool_call]
    SPL --> SW2[worker · tool_call]
    SW1 --> SJP2{{judge_post}}
    SW2 --> SJP2
  end

  classDef judge fill:#fef3c7,stroke:#f59e0b,color:#78350f;
  classDef plan fill:#dbeafe,stroke:#3b82f6,color:#1e3a8a;
  classDef work fill:#ecfdf5,stroke:#10b981,color:#064e3b;
  classDef delegate fill:#fce7f3,stroke:#ec4899,color:#831843;
  class JPRE,JPOST,PJ,WJ1,WJ2,WJ3,SJP,SJP2 judge;
  class PL,SPL plan;
  class W1,W3,SW1,SW2 work;
  class W2 delegate;
```

**读法**：

1. **ChatFlow 层**承载用户视角的对话——简单问题一个 ChatNode 就够，复杂任务
   的 ChatNode 被标为 `auto_plan`，钻进去就是它自己的 WorkFlow。分支 / 合流 /
   压缩都在这一层发生。
2. **WorkFlow 层**是 agent 内部解题的 DAG。从一个 ChatNode 出发，先过
   **judge_pre** 确认任务明确、缺失的输入先要补齐；然后 **planner** 把任务拆
   成若干 worker 组成的有向图；再过 **plan_judge** 审核这份分解是否合理。
3. **并行执行**：计划通过后，所有同层 worker 一起跑——tool_call 直接做工具调
   用，`sub_agent_delegation` 则递归展开成一张嵌套 WorkFlow（深度不限，每层
   都有自己的 pre/plan/post 三级审核）。
4. **收敛审核**：每个 worker 的产出都会被自己的 `worker_judge` 审一次，最后
   由 **judge_post** 统一判决目标是否达成。不达成就根据 `redo_targets` 精确
   重跑受影响的子树，而不是整个 workflow 重来。

一条贯穿的原则——**所有 judge / planner / compact / merge 本身也都是
WorkFlow fixture**，不是 engine 里的特殊逻辑。这意味着三层审核的 prompt、
分解策略、重试判据都是用户可以直接审视并替换的 YAML 模板。

---

## 核心概念

| 概念 | 是什么 |
|---|---|
| **ChatFlow** | 外层 DAG。节点是 `ChatNode`——一轮 user/agent 对话、或者特殊的 compact / merge 节点。边是 `parent_ids`，fork 就是共享 parent 开新 ChatNode。 |
| **ChatNode** | 对话中的一轮。承载一条 `user_message`、一条 `agent_response`，以及产生这条回复的那个*内层* WorkFlow。也可以是 compact 或 merge 节点。 |
| **WorkFlow** | 内层 DAG。节点是代表一个 agent 工作单元的 `WorkNode`。 |
| **WorkNode** | 类型之一：`llm_call` / `tool_call` / `judge_call` / `sub_agent_delegation` / `compact` / `merge`。执行完后变为实线（冻结、不可变）。 |
| **Planner** | 递归分解器。把一个 auto_plan 的 ChatNode 展开成 WorkFlow DAG，并在 judge_pre / judge_during / judge_post 各关卡上插入检查点。 |
| **MemoryBoard** | 每个 ChatNode / WorkNode 各自产出一条 brief（简要描述 + source_kind + 源节点 id），组成 ChatBoard / WorkBoard 两块可检索的看板，供 judge、compact、get_node_context 这类下游消费者按 id 召回原文。 |
| **执行模式** | 每节点可选：`native_react`（单一 ReAct 循环）/ `semi_auto`（显式 plan 阶段，一次性执行）/ `auto_plan`（递归 planner + judge 驱动重试）。 |

---

## 目前已落地的能力

### 对话 DAG
- [x] 任意 ChatNode 右键 → "从此处分支" 开 fork
- [x] **两节点合流**，用 VSCode-compare 式的两步操作（"选中待合流" → "与待合流项
      合并"；拖拽/ESC/点空白可取消）。LCA 感知：root→LCA 的公共前缀只喂给
      模型一次，只有 LCA 之后的两条分支后缀才进入 merge 上下文。
- [x] **Compact** Tier 1（自动，触发于 llm_call 前）+ Tier 2（手动或自动，
      ChatFlow 级）。复用同一套 compact 模板，生成带可见快照的 ChatNode，
      保留 tail 消息。
- [x] Joint-compact：当两条待合流分支合起来超预算时，会在两个源节点和 merge
      节点之间材料化一个可见的 joint-compact ChatNode，而不是默默对每条分支做
      一次隐藏的预压缩。
- [x] **Pack**（ChatFlow 层打包）：对一段连续 ChatNode 范围做主题摘要，生成
      带 `packed_range` 的可见 pack ChatNode，支持嵌套打包 + 跨 fork/merge
      打包；UI 上点"开始打包"两点选中范围，悬停 pack 节点高亮其打包范围
- [x] **折叠 / 展开（fold/unfold）**：右键 pack 或 compact 节点 → 折叠此范围 →
      合成一个 "折叠了 N 个节点" 的代理节点挂在 host 上游，被折节点消失、
      fork / pack 子节点通过 fold 的 top / right / bottom handle 重路由
      （"内部分支" vs "末端分支" vs "pack 挂下面" 视觉区分）。折叠状态和 fold
      节点位置 per-chatflow 持久化到 localStorage，刷新不丢
- [x] retry / cancel / delete 级联处理
- [x] 分支导航（↑↓ 切兄弟，跳转到 parent/child）
- [x] 多 parent 的 merge ChatNode 在 canvas 上渲染成汇流形态

### WorkFlow 引擎
- [x] 三种执行模式（`native_react` / `semi_auto` / `auto_plan`），ChatFlow 级
      默认值 + 每 ChatNode 可覆盖
- [x] 递归 planner：`plan.yaml` / `planner.yaml` / `planner_judge.yaml` 模板化
      的 "分解 → 执行 → judge → retry" 循环
- [x] 并行同层调度——每一层就绪的 WorkNode 一批并发执行
- [x] 子 agent 委派：嵌套 WorkFlow + 向上冒泡的 halt + 向上转发的 SSE 事件
- [x] Judge 三段式（`pre` / `during` / `post`），结构化 verdict（JSON Schema +
      强制 tool-use 双保险）
- [x] Ground-ratio 保险丝：WorkFlow 长时间没有 tool_call 就熔断
- [x] **Delegation-depth 保险丝**：递归规划最多分解 2 层，更深强转 atomic——
      防止一条多段 prompt 炸成 200+ 节点的失控树
- [x] **Decompose 部分成功聚合**：decompose group 全部成员 terminal
      （SUCCEEDED / FAILED / CANCELLED）即启动聚合 judge_post，partial 结果
      配合带具体失败原因的 halt message 推给用户
- [x] Retry budget + redo_targets（重开并重跑受影响的子树）
- [x] Tool-loop 预算守卫
- [x] Pending user-prompt：agent 可以在流程中主动提问并暂停，用户回复后流程继续

### 上下文管理
- [x] 上下文窗口查询缓存（per-provider / per-model，读取真实元数据——Ark 131K、
      Anthropic 200K 等）
- [x] Compact 触发阈值 + 目标占比不变式（总和 ≤ 100%）
- [x] Compact 循环保险丝（避免在 summary 之上递归压缩）
- [x] **Compact 输入溢出 preflight**：当 compact 自身输入将超出 compact_model
      的上下文窗口时，自动把 ancestor 消息替换为 `[node:<id>] <MemoryBoard
      brief>` 的引用形式（Pack 同款 citation），保证 compact worker 总能跑完，
      下游 worker 需要细节时走 `get_node_context` 回溯
- [x] 带 ChatNode id 前缀的标签化上下文，方便 compact worker 引用
- [x] 结构化 citation + coverage 兜底：当 compact / merge LLM 忘了引用源
      节点 id 时，engine 会把未被引用的节点的原文尾巴截断后追加进去，保证
      下游上下文不会成为孤儿
- [x] **MemoryBoard**：ChatBoard（ChatNode 级）+ WorkBoard（WorkNode 级）
      两块 brief 看板；judge / compact / reader-skill 按 id 召回原文
- [x] **粘滞遗忘（sticky-restore）**：`get_node_context` 命中会把源节点钉进
      当前 ChatNode 的 `sticky_restored`，并沿对话链逐轮衰减；fork 后
      独立衰减、merge 时取 MAX，下一轮 compact 不会再次把它压掉
- [x] `inbound_context` 分段预览 API：ChatFlow 右栏把即将喂给 LLM 的上下文
      按 summary_preamble / preserved / ancestor / **pack_summary** /
      sticky_restored / current_turn 分段展示，合成段与真实段视觉上区分开；
      compact 节点气泡内以结构化 **CBI bullets** 列出被折叠的祖先节点，
      点击可跳转

### Provider + 工具
- [x] OpenAI 兼容 provider（Volcengine / Ark / Ollama / OpenAI）
- [x] Anthropic 原生（用于 Claude 工具调用）
- [x] `provider_sub_kind` 白名单控制每个 provider 可用的采样参数
- [x] 按调用类型的模型覆盖（judge / tool-call 可以用更便宜的模型）
- [x] 内置工具：Bash / Read / Write / Edit / Glob / Grep / Tavily 搜索
- [x] MCP 客户端（基础版）
- [x] `get_node_context` skill——按 id 拉任意 ChatNode / WorkNode 的上下文

### UX
- [x] React Flow canvas：sticky notes、compact 徽章、merge 徽章、等待用户
      高亮、active-work 面板
- [x] 画布右下角 **MemoryBoard 浮窗**（ChatFlow / WorkFlow 通用）：列出当前
      flow 的所有 brief，点击条目跳转到源节点
- [x] ChatFlow 设置：执行模式、default / judge / tool-call / compact 模型、
      compact 阈值、ground-ratio 阈值

  ![ChatFlow 设置面板：按类别（判官 judge / 工具调用 tool_call / 压缩 compact）
  分别指定模型与采样参数，compact 触发/目标阈值两端绑定且总和 ≤ 100%。
  ](docs/images/05-settings-panel.png)
- [x] ConversationView：compact / merge 气泡、token 用量、复制、markdown 渲染
- [x] i18n（en-US + zh-CN）——所有 fixture 模板 + 引擎熔断消息都按语言各一份，
      包括 `min_ground_ratio` / `judge_retry_budget` / `judge_pre` /
      `judge_post` 的所有用户可见文本
- [x] MemoryBoard 浮窗顶部拖动条调整高度 + 一键最大化/还原（70vh ↔ 256px）
- [x] ChatNode 卡片显示执行模式徽章（Native ReAct / Auto Plan）
- [x] 节点拖动位置持久化：画布上手动摆位后，刷新、切回重新打开 CF 都保留；
      窗口关闭 / 切后台用 `fetch({keepalive: true})` 兜底 flush，
      防抖 500ms 内刷新也不丢
- [x] **浏览器状态持久化**：刷新后当前打开的 ChatFlow、节点 focus、WorkFlow
      drill 路径、fold 状态和 fold 节点位置全部恢复。`agentloom:ui:*` /
      `agentloom:fold:*` 分域 localStorage，hydrate 时先对 live chatflow
      reconcile（跳过已删节点）再还原
- [x] 结构化 JSON 输出（provider / model 两层 `json_mode`）

### 基础设施
- [x] docker-compose 起 Postgres（async SQLAlchemy）+ Redis
- [x] Alembic 迁移
- [x] SSE 事件总线：按 workflow 订阅 + 嵌套转发
- [x] 分层 token-bucket 限流
- [x] **启动期 orphan sweep**：进程重启时扫全部 chatflow，把 `running` /
      `retrying` / `waiting_for_rate_limit` 的孤儿节点（引擎死亡时留下的
      幽灵态）转成 `FAILED`，避免 UI 里永远 "在跑"
- [x] **Frozen-guard exempt 不变量测试**：`test_frozen_guard_exempts.py`
      正反两面固定 UI-only 字段（position / sticky / pending_queue）在
      frozen 节点上放行、语义字段必触发；防未来加新字段时踩拖动丢失那类坑
- [x] Pytest：后端 **362** 个单测 + 前端 **78** 个单测全绿，collection 干净

---

## 待开发

下面这些是已经设计过、但还没动手（或只完成了 scaffolding）的方向：

- [ ] **WorkFlow 层 pack**——当前 pack 只做了 ChatFlow 层（对话范围打包），
      WorkFlow 层对应"把一段 agent 活儿的产出打包成可交付件（文档、patch、
      结构化报告）"的形态尚未做。
- [ ] **认知节点 ReAct DAG 展开**——planning / pre-check / monitoring /
      post-check 这批"认知 WorkNode"统一支持 ReAct 式 DAG 展开（两端 cognitive，
      中间 tool_call），跟 MCP runtime（M7.5）搭车。当前止血手段是在 planner
      prompt 里注入 capability 白名单。
- [ ] **Judge 深读 skill**——judge_post 没法按需拉取 sibling 全文。等 MCP /
      skills 就绪后包成一个显式 skill，并处理好 tool_result 溢出时的回退路径。
- [ ] **Engine 动作 = tool-use**——把 planner.decompose、judge verdict 这些
      engine 自产动作统一改写成显式的 tool_call，让它们和用户工具/内置工具走同
      一套 schema + 日志 + blackboard 写入路径。MCP runtime 落地之后再动。

---

## 设计理念

三条承诺贯穿了大部分设计决策：

1. **执行即冻结，迭代靠分支。** 一个节点一旦执行完就不可变。想换思路？开分支——
   之前那次尝试会作为历史留在 canvas 上。这让所有实验都可审计，也让"回滚"
   变成一等导航操作，而不是破坏性编辑。

2. **DAG 优于线性。** 真实的研究过程从来不是单线程，而是多个并行思路偶尔汇流。
   fork 的树形 + merge 算子把 canvas 变成 DAG，历史是结构性的而不是时间性的。
   正是这个结构让并行分支、跨分支合流、带源节点引用的压缩摘要成为可能。

3. **Engine 动作就是可复用的计划，不是硬编码。** Compact、merge、judge、
   planner——每一个都是一份 YAML fixture 实例化成的真 WorkFlow，不是 engine 里
   的特殊逻辑。用户可以（未来也能）直接审视和修改这些 fixture；平台自己吃自己的
   狗粮。

---

## 开发环境

```bash
cp .env.example .env
# 按需填入 VOLCENGINE_API_KEY、TAVILY_API_KEY、ANTHROPIC_API_KEY

# 启动 postgres + redis
docker compose up -d postgres redis

# 后端（热重载）
cd backend
pip install -e ".[dev]"
uvicorn agentloom.main:app --reload

# 前端（另开一个终端）
cd frontend
npm install
npm run dev
```

健康检查：`curl localhost:8000/health` → `{"status":"ok","version":"0.1.0"}`
前端：`http://localhost:5173`。

## 测试

```bash
make test           # 后端 unit + integration
make test-smoke     # 实打 API 的 smoke（需要环境变量里的 key）
make test-e2e       # playwright
cd frontend && npx vitest run   # 前端单测
```

## 目录结构

```
backend/
  agentloom/
    api/          HTTP 路由（chatflows / workflows / providers / settings）
    db/           SQLAlchemy 模型 + 异步仓储
    engine/       WorkFlow + ChatFlow 执行引擎
    providers/    OpenAI 兼容 + Anthropic 原生适配
    templates/    YAML fixture（plan / planner / judge / worker / compact / merge / title_gen）
    tools/        Bash / Read / Write / Edit / Glob / Grep / Tavily / node_context
    mcp/          MCP 客户端
    rate_limit/   分层 token bucket
  alembic/        数据库迁移
frontend/
  src/
    canvas/       React Flow canvas、ConversationView、节点卡片
    components/   设置、对话框、ribbon
    i18n/         zh-CN + en-US 语言包
    store/        Zustand store（chatflowStore、preferencesStore）
tests/
  backend/{unit,integration,smoke,system}
  frontend/   （与 src 共置的 `*.test.ts`）
docs/
  devlog.md     <-- 开发日志权威记录
```

## 状态

快速迭代中的 MVP。ChatFlow / WorkFlow DAG + planner + compact + merge 核心
都已落地。详见 [`docs/devlog.md`](docs/devlog.md)——每一个里程碑、每一次设计
权衡、每一个踩过的 bug 及其修复都在那里。

---

## 开发日志

想看完整叙事——设计决策、考虑过但否决的方案、集成时冒出来的 bug、功能落地的
顺序——请看：

**→ [`docs/devlog.md`](docs/devlog.md)**
