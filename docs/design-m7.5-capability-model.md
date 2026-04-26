# Agentloom M7.5 — Capability Model + MCP-Native Runtime + Tool Registry 设计文档

> 状态：草案 v0.1 · 日期：2026-04-26 · 起点 commit：`2e1b0a7` (main)
> 前置阅读：`project_agentloom_capability_model.md` / `_cognitive_react_dag.md` / `_engine_actions_as_tools.md` / `_shell_sandbox_backlog.md` / `_judge_blackboard_skills.md` / `_depth_over_breadth.md` / `_drill_down_ids.md`

---

## 0. TL;DR

M7.5 是 7 个 deferred backlog 的共同前置里程碑。本里程碑只做"基础设施 + 一次性架构对齐"，不做完整功能落地：

- **必做（基础设施）**：tool side_effect 元数据、WorkFlowNode 双 capability 字段（effective / inheritable）、tool registry per-call 过滤管线、capability_request 结构化信号、inheritable catalog 注入到 judge/planner、cognitive ReAct DAG 调度分支。
- **顺路解锁但本里程碑不实施**：fan-in 拉式聚合、judge_post 深读 sibling skill、跨 ChatFlow drill-down、独立 PowerShellSkill / WorkSpace sandbox、engine actions 全面 tool_use 化。
- **PR 数**：拆 8 个，每个 ≤ 600 行 diff，按依赖链顺序 ship。第一个 PR 只做 schema + 元数据，约 ~250 行 diff。

---

## 1. 范围定义

### 1.1 本里程碑 IN

| 序号 | 内容 | 对应 memory |
|---|---|---|
| A | `Tool.side_effect: none\|read\|write` 元数据（built-in + MCP adapter） | capability_model §How |
| B | `WorkFlowNode.effective_tools` / `inheritable_tools` schema 字段（`list[str]` 工具名集合） | capability_model 主体 |
| C | `WorkFlowNode.capability_request: list[str]` 结构化升级信号槽位 | capability_model §升级通道 |
| D | `ChatFlowEngine._resolve_tool_catalog(chatflow)` + `_compose_system_envelope` 注入 inheritable catalog 到 judge/planner | capability_model §Inheritable tool catalog |
| E | tool registry per-WorkNode 过滤的统一管线（替换现 `definitions_for_constraints` 的 ad-hoc gate） | capability_model 主体 |
| F | Layer1 cognitive 节点的 ReAct DAG 调度分支（cognitive_start → tool_call×N → cognitive_final），仅启用 `judge_pre`，其余三类只搭骨架 | cognitive_react_dag |
| G | `judge_pre.extracted_capabilities` 解释规约：从"自然语言能力名"改为"tool registry name 集合"，并消费它写到 `WorkFlow.inheritable_tools` | capability_model + cognitive_react_dag |
| H | engine actions as tool_use 的入口最小化：仅把 `judge_verdict` 切到 tool_use（已有 `judge_verdict_tool_def` 雏形）+ planner mount 三个 verdict tool（atomic / decompose / infeasible）；JSON fallback 保留 | engine_actions_as_tools |

### 1.2 本里程碑 OUT（解锁但 deferred）

| 序号 | 内容 | 触发条件 |
|---|---|---|
| O1 | fan-in aggregator 拉式 `get_node_context` 聚合（替代 `_inject_upstream_outputs_into_ready_children`） | 等本里程碑 + brief 质量评估 |
| O2 | `read_node_detail(node_id, field)` skill 给 judge_post 深读 sibling | 等 capability model + tool_result Tier 0 cap |
| O3 | 跨 ChatFlow / WorkSpace BoardItem 总览 brief | 等 WorkSpace 多租户 |
| O4 | 独立 BashSkill / PowerShellSkill 替代 ShellExec 单工具 | 等本里程碑 + 真有 Windows 用户 |
| O5 | WorkSpace 级 sandbox（容器 / chroot） | 等多租户安全边界需求 |
| O6 | judge_during 与 judge_post 的 ReAct DAG 展开（仅 judge_pre 在 F 中启用） | 等 judge_pre 验证稳定 |
| O7 | drill-down ReAct chain 渐次 forget footer | 等 F 落地后 |

### 1.3 边界判定原则

- 凡是会**改 schema / 改持久化迁移 / 改 capability 流向**的，必须放进本里程碑一次做完，否则后续返工。
- 凡是只是**新增一个 tool / 新增一类 skill / 新增 UI**的，原则上 deferred；M7.5 仅证明基础设施支持得了它们。

---

## 2. Capability Model Schema

### 2.1 `Tool` 元数据扩展

`backend/agentloom/tools/base.py` 的 `Tool` ABC 增加：

```python
class SideEffect(str, Enum):
    NONE = "none"   # 不接外部资源，纯计算/查询内存（罕见）
    READ = "read"   # 只读外部资源（fs, http GET, registry lookup）
    WRITE = "write" # 任何会修改外部状态的操作（fs write, http POST, exec）

class Tool(ABC):
    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}
    side_effect: SideEffect = SideEffect.WRITE   # 默认偏保守，逼新工具显式声明
    ...
```

**built-in tools 标注**（一次性，~10 行 diff per file）：
- `Read` / `Glob` / `Grep` / `GetNodeContext` / `MemoryBoardLookup` → `READ`
- `Write` / `Edit` → `WRITE`
- `Bash` → `WRITE`（保守。未来 ShellExec 拆分后 read-only 子集可以 `READ`）

**MCP adapter** (`backend/agentloom/mcp/tool_adapter.py::MCPRemoteTool.__init__`) 增加 `side_effect` 参数，默认 `WRITE`；从 MCP server 的 `annotations.readOnlyHint` 读（MCP 0.1.0+ 协议字段，不存在则保守 `WRITE`）。

### 2.2 `WorkFlowNode` capability 字段

`backend/agentloom/schemas/workflow.py` 的 `WorkFlowNode` 增加：

```python
# 替代/拓展 ToolConstraints（保留作为 v1 deny-list 兼容）
effective_tools: list[str] | None = None
# None = "继承 enclosing WorkFlow 的 inheritable_tools"（向后兼容）
# []   = 显式空（无工具，例如 monitoring 节点）
# [...] = 显式工具名白名单

inheritable_tools: list[str] | None = None
# 仅 PRE_JUDGE / PLAN / DELEGATE 节点应填；其余节点无意义留 None。
# 含义：本节点 spawn 子节点时允许下传的工具集上限。

capability_request: list[str] = Field(default_factory=list)
# 结构化升级信号。execution / monitoring 节点写入；
# planning 重分配时读取并清空。
```

**与 `ToolConstraints` 的关系**：保留 `tool_constraints` 字段不删（v1 用户编辑的 ChatFlowSettings 仍走 deny-list 路径），但**新增**两个字段在引擎层优先生效。规则：
1. 节点的"实际 visible tools" = (`effective_tools` 决定的白名单) ∩ (chatflow 的 `disabled_tool_names` 取反) ∩ (workspace `globally_disabled` 取反) ∩ (`tool_constraints.deny` 取反)。
2. `effective_tools is None` 时 fallback 到当前 `definitions_for_constraints` 行为，保证既有节点能跑。

### 2.3 `WorkFlow` capability 字段

`backend/agentloom/schemas/workflow.py` 的 `WorkFlow.capabilities` **改名 + 重定义**：

- 当前：`list[str]`，存自然语言能力名（"web_search", "code_execution"），来源是 `judge_pre.extracted_capabilities`。
- M7.5 后：拆成两个字段
  - `inheritable_tools: list[str]`（替代旧的 `capabilities` 字段）— 工具 registry name 集合，是本 WorkFlow 内所有节点可用工具的上限。`judge_pre` 写入，planner / 子 WorkFlow 读取。
  - `capabilities_origin: list[str]`（保留旧字段）— 自然语言痕迹，便于 UI 展示和 fixture 回溯，无引擎语义。

**迁移**：DB migration `00xx_workflow_inheritable_tools`：从旧 `capabilities` 列拷一份到 `capabilities_origin`，新建空 `inheritable_tools` 列。fixtures 同步更新——`judge_pre.yaml` 输出 schema 增加 `extracted_inheritable_tools: [str, ...]`（从 inheritable catalog 中精确选名），保留 `extracted_capabilities` 作为自然语言备注。

### 2.4 `JudgeVerdict` 新字段

`backend/agentloom/schemas/common.py::JudgeVerdict`：

```python
# 新增（仅 judge_pre 写入）
extracted_inheritable_tools: list[str] | None = None
# judge_pre 从 chatflow_tool_catalog 中精选的工具名，对应 WorkFlow.inheritable_tools

# 新增（仅 judge_during / monitoring 触发 re-plan 时写入）
capability_escalation: list[str] = Field(default_factory=list)
# 把 execution 节点 capability_request 收集到的工具名整理成 re-plan 信号
```

`judge_parser.py::_VARIANT_SCHEMAS` 同步扩展。

### 2.5 默认权限矩阵（落到代码层）

Layer1 节点 spawn 时由 `_spawn_*` 系列函数赋默认值：

| 节点 (role) | step_kind | effective_tools 默认 | inheritable_tools 默认 |
|---|---|---|---|
| PRE_JUDGE | judge_call | side_effect ∈ {none, read} 的 catalog 全集 | catalog 全集（由 judge_pre 写 `WorkFlow.inheritable_tools`） |
| PLAN | draft (with planner_grammar) | None（不调工具，但 prompt 看 inheritable catalog） | 父 WorkFlow.inheritable_tools 全集 |
| PLAN_JUDGE | judge_call | side_effect ∈ {none, read} 的 catalog 全集 | ∅ |
| WORKER | draft / tool_call | planner 分派的子集 | self.effective_tools 子集（DELEGATE 才有意义） |
| WORKER_JUDGE | judge_call | self.parent's effective_tools 去掉 write | ∅ |
| POST_JUDGE | judge_call | enclosing WorkFlow 的 PRE_JUDGE.effective_tools（read-only） | ∅ |

注：DELEGATE 节点的 `inheritable_tools` 是 sub_workflow 子节点们 effective 的上限，由 planner 主动收窄。

---

## 3. Tool Registry 重构

### 3.1 当前结构

`backend/agentloom/tools/registry.py::default_registry()` 静态注册 8 个 built-in tools；`mcp/runtime.py::init_runtime()` 在 lifespan 启动时调用，并通过 `add_source` 把每个 MCP server 的 tools 也 register 进同一个共享 registry。`workflow_engine.py::_invoke_and_freeze` 中通过 `definitions_for_constraints(node.tool_constraints)` 过滤后扔给 provider。

### 3.2 改造目标

引入"per-call tool resolution"作为唯一入口，把现有的多个 ad-hoc 过滤点（`_disabled_tool_names`, `definitions_for_constraints`, `tool_constraints.deny`）合一：

```python
# tools/registry.py 新增
class ToolRegistry:
    def resolve_for_node(
        self,
        *,
        node_effective: list[str] | None,
        chatflow_disabled: frozenset[str],
        workspace_globally_disabled: frozenset[str],
        side_effect_filter: set[SideEffect] | None = None,
        legacy_constraints: ToolConstraints | None = None,
    ) -> list[Tool]:
        """单一过滤入口。返回 LLM 可见的 Tool 列表，按确定顺序。"""
```

- `node_effective is None` → 沿用 `legacy_constraints` 路径（向后兼容）。
- `node_effective is not None` → 严格白名单 ∩ chatflow_disabled 取反 ∩ workspace_globally_disabled 取反 ∩ side_effect_filter（如有）。

### 3.3 `_invoke_and_freeze` 改造

`backend/agentloom/engine/workflow_engine.py:1316-1325` 那段过滤逻辑替换为：

```python
tool_objs = self._tools.resolve_for_node(
    node_effective=node.effective_tools,
    chatflow_disabled=self._disabled_tool_names,
    workspace_globally_disabled=self._workspace_globally_disabled,
    side_effect_filter=self._side_effect_filter_for(node),
    legacy_constraints=node.tool_constraints,
)
tool_defs = [ToolDefinition(**t.definition()) for t in tool_objs]
```

`_side_effect_filter_for(node)`：
- POST_JUDGE / PRE_JUDGE / WORKER_JUDGE 默认 `{NONE, READ}`
- 其余 None（不强加）

### 3.4 MCP server 注册：保留现状

MCP adapter 已经把每个远程工具包装成 in-process `Tool`，名字带 `mcp__<server>__` 前缀。本里程碑**不**进一步抽象为"capability provider 接口"——抽象成本高、收益低（MCP 协议本身就是抽象层）。

仅做小改动：`mcp/tool_adapter.py::MCPRemoteTool.__init__` 多接收 `side_effect` 参数（来自 MCP server 的 `readOnlyHint` 注解），默认 WRITE。

### 3.5 inheritable catalog 注入到 judge / planner

实现 capability_model memory §Inheritable tool catalog 的设计：

1. 新增 `ChatFlowEngine._resolve_tool_catalog(chatflow) -> str`，渲染 markdown 列表（B 档：name + 一句 description）。
2. `execute()` kwarg 增加 `chatflow_tool_catalog: str`，传到 `WorkflowEngine`。
3. **删除** `_maybe_prepend_runtime_note` 中的 `if not tool_defs: skip` gate，改名为 `_compose_system_envelope`：
   - worker call (tool_defs 非空)：仅 runtime note。
   - judge / planner call (tool_defs 空但 catalog 非空)：runtime note + `\n---\n` + catalog block。
4. UI 不改（catalog 是 tool registry 派生事实，自动渲染不可编辑）。

---

## 4. 认知节点 ReAct DAG 展开

### 4.1 调度判定

新增引擎方法 `_should_expand_to_dag(node) -> bool`：仅当：
- `node.role in {PRE_JUDGE, PLAN_JUDGE, WORKER_JUDGE, POST_JUDGE}`
- 且 `node.effective_tools` 非空（节点确实拿到了 read-only 工具集）
- 且 chatflow 配置 `cognitive_react_enabled = True`（默认 True）

### 4.2 DAG 形态

```
cognitive_start (judge_call, expose_tools=False, atomic_or_dag schema)
   │
   ├─ atomic 分支 → 直接吐 verdict（兼容现路径）
   │
   └─ dag 分支 → spawn tool_call 链
        ├─ tool_call_1 (READ-only)
        ├─ tool_call_2 (READ-only)
        ├─ ...
        └─ cognitive_final (judge_call, consume tool_call outputs, 吐 verdict)
```

**第一个 cognitive 节点**走"atomic vs DAG"判定：增加新 schema 字段：

```python
# judge_parser._VARIANT_SCHEMAS 中 PRE / POST / DURING 共享新增
"recon_plan": {
    "type": "object",
    "properties": {
        "mode": {"enum": ["atomic", "recon"]},
        "tool_calls": {"type": "array", "items": {...}},
    }
}
```

`mode == atomic` → 直接 emit verdict；`mode == recon` → 引擎为每个 `tool_calls[i]` spawn 一个 `step_kind=tool_call` 节点（限 side_effect ∈ {none, read}），全部完成后 spawn 一个新的 cognitive_final judge_call 节点接收所有 tool_call outputs 并 emit verdict。

### 4.3 中间节点约束

`tool_call` 子节点的 `effective_tools` = `cognitive_start.effective_tools`（已经是 read-only 子集）；禁止 `step_kind` 为 `draft / delegate`，由 `_spawn_recon_tool_call` 强制。

### 4.4 本里程碑只启用 `judge_pre`

`PLAN_JUDGE / WORKER_JUDGE / POST_JUDGE` 仅在引擎层添加调度分支但默认走 atomic（feature flag `cognitive_react_judges_enabled = {pre: True, plan: False, worker: False, post: False}`）。
- 原因：四类一起改面太大，违反 small-steps 原则。
- judge_pre 验证稳定后，逐 PR 解锁其余三类（属于 deferred 跟进，不在本里程碑 PR 数内）。

---

## 5. Engine Actions as Tool_Use

### 5.1 现状

- `judge_parser.py::judge_verdict_tool_def(variant)` 已生成 tool 定义。
- 但 `_run_judge_call` 当前走 `json_schema=judge_verdict_json_schema(...)` 路径，未实际把 verdict 当 tool_use。
- planner 走 `planner_grammar_schema()` JSON 路径，没有 mount tool_use。

### 5.2 本里程碑改造：双轨

**Tier 1（强 provider：`openai_chat` / `volcengine` / `anthropic` / `ark`）**：
- judge / planner 同时下发 tools + json_schema：
  - judge: `tools=[judge_verdict_tool_def(variant)]` + `forced_tool_name="judge_verdict"` + `json_schema=judge_verdict_json_schema(variant)`
  - planner: `tools=[atomic_tool_def, decompose_tool_def, infeasible_tool_def]` + `tool_choice=any` + `json_schema=planner_grammar_schema()`
- parser 优先读 `output_message.tool_uses[0].arguments`，缺失则 fallback 到 content JSON。

**Tier 2（弱 provider：`llamacpp` / `ollama` / `volcengine` 之外的 openai 兼容）**：
- 由 `_RESPONSE_FORMAT_COEXISTS_WITH_TOOLS` 已有 gate 自动二选一；本里程碑不改 gate。
- 弱 provider 仍走纯 JSON schema 路径，parser 不变。

**为什么不一刀切到 tool_use**：memory `engine_actions_as_tools.md` 明确 user 要求 "tool-use preferred, JSON fallback"；Ollama tool-use parity 仍不稳。本 PR 只把强 provider 路径打开，弱 provider 继续 JSON。

### 5.3 涉及文件

- `engine/judge_parser.py`：扩 `parse_judge_from_tool_args` 已存在，新增 `parse_planner_from_tool_args`。
- `engine/recursive_planner_parser.py`：新增 `planner_tool_defs() -> list[ToolDefinition]`（atomic / decompose / infeasible）。
- `engine/workflow_engine.py::_run_judge_call` (~行 2125-2135)：`forced_tool_name="judge_verdict"` + 加 `override_tools=[...judge_tool_def]`。
- `engine/workflow_engine.py::_run_llm_call` (~行 1396)：planner 路径加 `override_tools=planner_tool_defs()`。

---

## 6. Fan-in 拉式引用（仅打地基）

### 6.1 本里程碑只做的事

仅给 `_inject_upstream_outputs_into_ready_children`（`chatflow_engine.py:4053`）增加 feature flag `aggregator_pull_mode: bool = False`：
- False（默认）：维持现状，推式注入 sub.inputs。
- True：改写为只注入 `brief + node_id` 列表，aggregator 通过 `get_node_context` 拉。

True 路径**不在本里程碑实施**，只确保以下前置就绪（这是本里程碑的真正意义）：
- `get_node_context` 已支持跨 ChatNode 跨 WorkFlow（drill_down_ids memory 已确认 shipped）。
- aggregator 节点 `effective_tools` 默认包含 `get_node_context`。
- worknode_catalog 在 `_after_delegation` 注入 sub.inputs 时附 brief（已部分有）。

### 6.2 跨 WorkFlow sibling access

memory `_depth_over_breadth.md` 明确"WorkFlow-layer sibling access 受 capability 白名单限制"。本里程碑做法：
- `get_node_context` 内部 lookup 不再单纯按 workspace_id 校验（当前 `tools/node_context.py:96-100`）；新增校验：
  - 如果 caller 是 aggregator 且目标是同 chatflow 的 sibling WorkNode → 允许。
  - 如果 caller 试图读跨 chatflow 节点 → 检查 `caller.effective_tools` 是否包含 `get_node_context.cross_chatflow`（虚拟权限位，新增）。

实现选项 A（推荐）：`get_node_context` 新增 `args.scope: "self_chatflow"|"cross_chatflow"`，scope=cross_chatflow 时检查 caller 的 effective_tools。
实现选项 B：保持单一工具，新增 `Tool.subcapabilities: list[str]`，effective_tools 中可写 `get_node_context.cross_chatflow`。

本里程碑选 **A**（实现简单，schema 不变）。

---

## 7. 跨 ChatFlow / WorkSpace BoardItem 访问

### 7.1 Drill-down 现状

memory `_drill_down_ids.md` 确认 commit `9e5dcba` 已 ship 同 ChatFlow 内 drill-down（inner_chat_ids + work_node_ids + footer）。

### 7.2 deferred 的部分（仅 capability 接入，不实施）

- **跨 ChatFlow 访问**：通过 §6.2 的 scope 参数 + capability 白名单实现；本里程碑只把 capability 钩子留好。
- **WorkSpace 视角同列 sibling brief**：依赖 WorkSpace 多租户成熟，不在本里程碑。

### 7.3 capability 接入点

`WorkFlowNode.effective_tools` 中可以出现以下"虚拟工具名"作为 capability 位（不对应真实 Tool 实例，只是 gate 字符串）：
- `get_node_context.cross_chatflow`
- `get_node_context.cross_workspace`（永远不发，留位）

`registry.resolve_for_node` 看到带 `.` 后缀的名字时跳过（不渲染到 LLM tool list），但 `get_node_context.execute` 内部读 `ctx.caller_effective_tools`（新字段，由引擎注入）做 gate。

---

## 8. 依赖关系图

```
                ┌─────────────────────┐
                │ A: side_effect 元数据│
                └─────────┬───────────┘
                          │
        ┌─────────────────┼─────────────────┐
        ▼                 ▼                 ▼
┌──────────────┐   ┌──────────────┐  ┌──────────────┐
│ B: WorkNode   │   │ E: registry   │  │ G: judge_pre │
│ effective/    │   │ resolve_for_  │  │ extract_tools│
│ inheritable   │   │ node          │  │ → workflow   │
└───────┬───────┘   └───────┬──────┘  └──────┬───────┘
        │                   │                 │
        └─────────┬─────────┴─────────────────┘
                  │
                  ▼
         ┌──────────────────┐
         │ D: catalog inject │
         │ to judge/planner  │
         └────────┬──────────┘
                  │
        ┌─────────┴─────────┐
        ▼                   ▼
┌──────────────┐   ┌──────────────────┐
│ C: capability │   │ F: cognitive    │
│ _request 信号 │   │ ReAct DAG (judge_pre)│
└───────┬───────┘   └────────┬─────────┘
        │                    │
        └─────────┬──────────┘
                  ▼
         ┌─────────────────┐
         │ H: judge_verdict │
         │ as tool_use      │
         └─────────────────┘
```

关键依赖：
- A 是所有的根（没有 side_effect，B/E/F 都没法做 read-only filter）。
- B 与 E 互依（schema 改 + 引擎过滤改要一起）。
- D 依赖 B（inheritable_tools 字段才能渲染）。
- F 依赖 D（cognitive 节点要看到 catalog 才知道 spawn 哪些 tool_call）。
- C 与 F 弱耦合（capability_request 只有走到 monitoring 时才会用到，本里程碑不打通 re-plan）。

---

## 9. PR 拆分顺序（8 个 PR）

每个 PR 独立可 ship，包含单测 + 手测剧本 + 回滚说明。所有 PR 在 same branch line ship；任一 PR 出问题可单独 revert，下游 PR 用 feature flag 兜底。

### PR 1 — `tool: side_effect metadata` (~250 行)

- **范围**：`Tool.side_effect: SideEffect` enum 新增；built-in 8 个 tools 标注；MCP adapter 接收参数（默认 WRITE）。
- **前置**：无。
- **验证**：单测 `test_tool_side_effect_default_is_write` + `test_builtin_read_tools_are_read` + `test_mcp_adapter_passes_through_readonly_hint`。
- **回滚**：纯加字段，删除即恢复。

### PR 2 — `schema: WorkFlowNode effective/inheritable + WorkFlow.inheritable_tools` (~400 行)

- **范围**：schema 增字段；DB migration `00xx_workflow_node_capability_fields`；`extracted_inheritable_tools` 加到 `JudgeVerdict`；fixtures 更新 zh/en `judge_pre.yaml`；`_apply_judge_pre_trio` 写入 `workflow.inheritable_tools`。
- **前置**：PR 1（fixture 里要列 read-only / write 标记）。
- **验证**：迁移 round-trip 测试；fixture 输出 schema 测试；`test_judge_pre_extracted_tools_propagates_to_workflow`。
- **回滚**：migration downgrade + 字段删除。

### PR 3 — `registry: resolve_for_node unified pipeline` (~350 行)

- **范围**：`ToolRegistry.resolve_for_node` 新方法；`_invoke_and_freeze` 切换；保留 `definitions_for_constraints` 作 deprecated；新增 `_side_effect_filter_for(node)` helper。
- **前置**：PR 1, PR 2。
- **验证**：单测覆盖 6 个节点 role × 3 个 side_effect 的过滤矩阵；E2E 跑一个 chatflow 确认 worker 看不到 Write（如果 effective_tools 不含）。
- **回滚**：feature flag `unified_resolve_enabled = False` 回退到旧 `definitions_for_constraints`。

### PR 4 — `engine: catalog inject to judge/planner (compose_system_envelope)` (~300 行)

- **范围**：`ChatFlowEngine._resolve_tool_catalog`；`_compose_system_envelope` 替换 `_maybe_prepend_runtime_note`；删除 `if not tool_defs: skip` gate；execute kwarg 传 catalog；`workflow_engine` 接收并组装。
- **前置**：PR 2（catalog 来自 `inheritable_tools`）。
- **验证**：v7 qwen36 fixture 重跑——judge_pre 不再幻觉 Read 调用（capability_model memory §Inheritable tool catalog 中的 regression 场景）；token 用量 +5-9K 在预期范围。
- **回滚**：`_compose_system_envelope` 回滚到旧 gate。

### PR 5 — `engine: capability_request signal slot` (~200 行)

- **范围**：`WorkFlowNode.capability_request` 字段；`tool_loop` 末尾如果 worker 在 ToolResult 里 emit `{"capability_request": [...]}` JSON 段则提取；judge_during 读取并写到 `JudgeVerdict.capability_escalation`；planner 在 re-plan 时清空。
- **前置**：PR 2。
- **验证**：单测 `test_worker_capability_request_propagates_to_judge_during`；fixture 一个故意缺 Write 的场景，确认信号被识别（不打通 re-plan，仅记录）。
- **回滚**：字段删除。

### PR 6 — `engine: judge_verdict as tool_use (strong providers)` (~350 行)

- **范围**：`_run_judge_call` 强 provider 路径加 `override_tools + forced_tool_name`；`judge_parser.parse_judge_from_tool_args` 整合；planner 同样切到 tool_use（`planner_tool_defs()` 新增）；`_RESPONSE_FORMAT_COEXISTS_WITH_TOOLS` 路径双轨。
- **前置**：PR 2（schema 已扩）。
- **验证**：ark / openai_chat 各跑一个 fixture 确认 verdict 来自 tool_use；llamacpp / ollama 仍走 JSON schema 路径（gate 已存在）；parser fallback 测试。
- **回滚**：feature flag `judge_tool_use_enabled = False` 退到 JSON-only。

### PR 7 — `engine: cognitive ReAct DAG (judge_pre only)` (~500 行)

- **范围**：`_should_expand_to_dag` + atomic vs recon schema；`_spawn_recon_tool_call_chain`；`_spawn_cognitive_final` 接收 tool_call outputs；feature flag `cognitive_react_judges_enabled.pre = True`，其余三类 False。
- **前置**：PR 1, PR 3, PR 4, PR 6。
- **验证**：跑 2026-04-21 S1 halt 那个场景重现的 fixture，judge_pre 在 recon 模式下用 Glob/Grep 之后再判 feasibility=ok（之前是 infeasible 因为臆想工具）。
- **回滚**：feature flag 关。

### PR 8 — `tools: get_node_context cross_chatflow scope + capability gate` (~300 行)

- **范围**：`get_node_context.parameters` 增加 `scope` 字段；`tool_context` 增加 `caller_effective_tools` 注入；scope=cross_chatflow 时校验 effective_tools 含 `get_node_context.cross_chatflow`；`registry.resolve_for_node` 跳过含 `.` 的虚拟权限位。
- **前置**：PR 1, PR 3。
- **验证**：单测 `test_cross_chatflow_denied_without_capability` + `test_cross_chatflow_allowed_with_capability`；E2E 一个 chatflow 内的 aggregator 拉同 chatflow sibling（默认放行）。
- **回滚**：scope 默认值保持现行为。

**总规模**：~2650 行 diff，分 8 PR，每 PR ≤ 600 行（PR 7 接近上限，必要时再拆 7a/7b）。

---

## 10. 风险与权衡

### 10.1 可能反悔的设计决定

1. **`effective_tools` 字段类型 `list[str] | None`**：用 `None` 表"继承父"看起来巧妙，但和 `[]`（显式无工具）的语义差容易引发 bug。如果一周内观察到 `_invoke_and_freeze` 出现 None vs [] 混淆，切到 `Optional[Capability]` dataclass 显式标记 `inherit_from_parent: bool`。
2. **`SideEffect.WRITE` 默认保守**：所有未标注的工具变 WRITE 后，monitoring / post_check 会看不到——这是设计意图（"显式标 read 才能进 read-only 集合"）但可能误伤 MCP server 上批量未标 readOnlyHint 的查询工具。如果出现，临时给特定 server 配 `default_side_effect: read` 覆写。
3. **catalog 注入双语**：`_resolve_tool_catalog` 当前不分语言。中文 ChatFlow 看到英文 description 会有点割裂。本里程碑不处理；UI 已 i18n 后再考虑工具 description 的多语言池。
4. **判 atomic vs recon 由 cognitive_start 自己判定**：弱模型可能永远选 atomic 跳过 recon，导致 DAG 路径几乎不走。本里程碑接受这个风险——judge_pre 单 LLM 现行 fixture 已经有"该用 grounding 时不用"问题；DAG 不会比现状更差。

### 10.2 Weak provider fallback

| 路径 | qwen36 / llamacpp 行为 | fallback |
|---|---|---|
| catalog 注入 | 不影响（system message 常规处理） | 无需 fallback |
| judge tool_use | gate drop tools，走 JSON schema | 已有 lenient parser |
| planner tool_use | 同上 | 已有 grammar schema |
| cognitive ReAct DAG | recon mode JSON schema 弱模型可能输出畸形 | atomic mode 兜底；DAG 路径首次 fail 即 fallback atomic |
| capability_request | 弱模型不会主动写信号 | judge_during 看到空字段不 re-plan，等 monitoring budget 触发 |

### 10.3 Token 成本

memory 估计 catalog 注入 +5-9K token / chatflow。验证：
- B 档（name + 一句 description）每条 ~50 token，built-in 8 + 平均 5 个 MCP = ~13 条 = ~650 token，乘 judge_pre + planner + plan_judge + judge_during + judge_post = 5 个 cognitive 节点 = ~3.3K token。
- 按 chatflow 平均 3 turn 算 ~10K token——比 memory 估计略高但仍在可接受范围。
- **分级建议**（如果 token 涨得厉害）：A 档（仅 name）作 fallback，env var `AGENTLOOM_CATALOG_VERBOSITY={a,b}` 控制。本里程碑默认 B，不预实现 A。

### 10.4 Memory 与代码不一致

逐项核对 memory：
- ✅ `_inject_upstream_outputs_into_ready_children` 仍在 `chatflow_engine.py:4053`（depth_over_breadth memory 准确）。
- ✅ `judge_verdict_tool_def` 已存在 `judge_parser.py:262`（engine_actions_as_tools memory 写"未做"，**实际有雏形**——建议更新该 memory）。
- ✅ `WorkFlow.capabilities` 字段已 ship（capability_model memory 提到的 inheritable 工具集还没字段，准确）。
- ✅ `judge_pre.yaml` 已要求 `extracted_capabilities`（capability_model memory 准确）。
- ⚠️ memory `_engine_actions_as_tools.md` 标注"10 days old"，写"今天用 hand-parsed JSON in judge_parser.py"，但 `judge_verdict_tool_def` 已加。建议 user 确认后更新此 memory，注明"tool def 已 ready，但 engine 路径未切"。

---

## 11. 第一步行动

**第一个要写代码的 PR：PR 1 — `tool: side_effect metadata`**

- **改动文件清单**：
  - `backend/agentloom/tools/base.py`：新增 `SideEffect` enum + `Tool.side_effect: SideEffect = SideEffect.WRITE`。
  - `backend/agentloom/tools/bash.py`：标 `side_effect = SideEffect.WRITE`。
  - `backend/agentloom/tools/files.py`：`ReadTool` → READ，`WriteTool` / `EditTool` → WRITE。
  - `backend/agentloom/tools/search.py`：`GlobTool` / `GrepTool` → READ。
  - `backend/agentloom/tools/node_context.py`：READ。
  - `backend/agentloom/tools/memoryboard_lookup.py`：READ。
  - `backend/agentloom/mcp/tool_adapter.py`：`MCPRemoteTool.__init__` 接收 `side_effect: SideEffect = SideEffect.WRITE` 参数；`bridge.py::connect_and_register` 从 MCP `tool.annotations.readOnlyHint` 读出来传入。
  - `tests/backend/unit/test_tool_side_effect.py`：新建，覆盖默认值、built-in 标注、MCP 透传。

- **预估行数**：~250 行 diff（含测试 ~80 行）。

- **PR 描述模板**：
  > Adds `side_effect` metadata to every Tool (none/read/write). Built-in tools opt in; MCP adapter passes through `readOnlyHint`. No engine consumer yet — registry still ignores the field. Foundation for M7.5 capability model (PR 2-8).

- **验证**：
  - 单测：`pytest tests/backend/unit/test_tool_side_effect.py`。
  - 手测：起一个本地 MCP server（如 mcp-server-fetch），确认 `mcp__fetch__fetch.side_effect == READ`（fetch 协议带 readOnlyHint=true）。

- **commit message**：`tool: side_effect metadata for capability model`

- **回滚**：直接 revert，无 schema migration、无字段消费。

---

## 12. 用户拍板项

实施前需要 user 拍板的开放问题：

1. **`WorkFlow.capabilities` vs `inheritable_tools` 的字段命名 / 迁移策略**：是把旧字段重命名为 `inheritable_tools` + 加 `capabilities_origin` 留痕，还是新增 `inheritable_tools` 与旧 `capabilities` 并存？前者干净但 migration 复杂，后者向后兼容但语义重叠。Plan agent 倾向重命名（参见 §2.3）。
2. **judge_pre.extracted_capabilities 的语义改变**：从"自然语言能力名（web_search）"改为"tool registry name（mcp__tavily__tavily_search）"。fixture 改动较大，UI 展示风格也变。是否接受？还是保留双字段（自然语言版本 + tool 名版本）？
3. **判 atomic vs recon 的归属**：当前设计是让第一个 cognitive 节点自己 emit `{mode: atomic|recon}`。另一种方案是引擎层 heuristic（看 effective_tools 是否非空 + 是否有 prior failure 信号）。前者更"灵活"但弱模型不可靠；后者更可控。
4. **engine_actions_as_tools memory 已部分过期**（`judge_verdict_tool_def` 已 ship 但 memory 未反映）。是否同意更新此 memory，注明 "tool_def ready, engine path not switched"？
5. **catalog 注入的 token 成本**（B 档 ~10K token / chatflow 3 turn）：是否需要 PR 4 一起把 A 档（仅 name）feature flag 也加上？memory 说"可接受"，但实测后可能需要分级。

---

## 13. 关键文件索引

实施时改动会聚焦在这些文件：

- `backend/agentloom/tools/base.py` — Tool ABC, SideEffect enum
- `backend/agentloom/tools/registry.py` — ToolRegistry.resolve_for_node
- `backend/agentloom/tools/{bash,files,search,node_context,memoryboard_lookup}.py` — built-in side_effect 标注
- `backend/agentloom/mcp/tool_adapter.py` — MCP side_effect 透传
- `backend/agentloom/schemas/workflow.py` — WorkFlowNode + WorkFlow capability 字段
- `backend/agentloom/schemas/common.py` — JudgeVerdict 新字段
- `backend/agentloom/engine/workflow_engine.py` — `_invoke_and_freeze` / `_run_judge_call` / `_run_llm_call` / `_should_expand_to_dag`
- `backend/agentloom/engine/chatflow_engine.py` — `_resolve_tool_catalog` / `_compose_system_envelope` / `_inject_upstream_outputs_into_ready_children` flag
- `backend/agentloom/engine/judge_parser.py` — `parse_judge_from_tool_args` / `_VARIANT_SCHEMAS` 扩 recon_plan
- `backend/agentloom/engine/recursive_planner_parser.py` — `planner_tool_defs()`
- `backend/agentloom/templates/fixtures/{zh-CN,en-US}/judge_pre.yaml` — 输出 schema 改造
