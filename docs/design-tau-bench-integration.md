# τ-bench × Agentloom 集成方案设计文档

> 状态：草案 v0.2（user 拍板）· 日期：2026-04-26 · Agentloom HEAD：`00e63b9`
> 落盘路径：`docs/design-tau-bench-integration.md`（gitignore 白名单 `!/docs/design-*.md` 命中，可 commit）
>
> **拍板的设计决定（v0.2 update）**：
> 1. tau_bench 来源：**B 子集 vendor**（不走 git+ install），仅 vendor `envs/` + `types.py` + `LICENSE` 进 `vendor/tau_bench/`。backend 零依赖污染（不装 litellm / 不动 openai pin）。
> 2. user-side LLM：**ark-code-latest**（免费档；论文不可直比，报告标注）。
> 3. runner 位置：**独立 distribution `agentloom_bench/`**（不收进 backend/scripts/）；runner conda env 装完整上游 tau_bench（含 user.py + litellm），与 backend 完全隔离。

---

## 0. TL;DR

- τ-bench 是 multi-turn、tool-driven、双 LLM（agent + simulated user）的 task benchmark；论文 (arXiv 2406.12045) + Sierra 公开仓库齐全，retail / airline 两 domain，每 domain ~115 / ~50 tasks。
- 集成核心做三件事：(a) 把 τ-bench 的 retail/airline tool catalog 包成 Agentloom `Tool` ABC 实例并**临时注册**到全局 registry（复用 MCP 已经走通的 register/unregister lifecycle）；(b) 在 Agentloom 进程外起一个独立 runner，由 τ-bench 自己的 `UserStrategy` 充当 simulated user，runner 通过 HTTP `POST /api/chatflows/{id}/turns` 推 turn；(c) 每 task 跑完后 runner 直接调 τ-bench 的 `env.calculate_reward()` 拿 1/0 + 软指标。
- 不走 M7.5 关键路径——本里程碑可以**完全在 capability model 之前**落地，只是日后 M7.5 重构 registry 时要把 τ-bench 的临时注册接口跟着平移。
- 第一个 PR：vendoring + smoke import 测试，估 < 200 行；后续 5 个 PR 依序铺出 scoped registry / user driver / runner CLI / batch report / CI。整套预算：retail task 0–9（10 个）≈ 30–75 min × 0.3M–0.9M tokens 跑完，免费档（ark-code-latest）能 cover。

---

## 1. τ-bench 是什么 / 不是什么

### 1.1 是什么

τ-bench 由 Sierra Research 2024 年提出（论文 arXiv:2406.12045，"τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains"）。结构：

- **环境 (Env)**：`tau_bench/envs/{retail,airline}/env.py` 暴露一个 mock 业务系统（订单、用户、物流、航班），内部维护一份 in-memory dict 数据库（`data/`）。`Env.reset(task_index) → initial_observation`，`Env.step(action) → EnvResponse(observation, reward, done, info)`，`Env.calculate_reward()` 在终态比对 ground truth hash + 检查 outputs。
- **任务 (Task)**：`tasks_train.py` / `tasks_test.py` 里每个 task 是 dataclass，字段 `annotator / user_id / instruction / actions / outputs`：
  - `instruction` 是写给 simulated user 的"角色 + 目标"自然语言（"Your name is Omar Anderson, return order #W6067464 via credit_card_4190576..."）。
  - `actions` 是 ground truth 的工具调用序列，仅用于 reward 比对的"重放"——agent 不应看到。
  - `outputs` 是必须出现在 agent 最终回复里的字符串列表（soft match）。
- **simulated user**：`tau_bench/envs/user.py::UserStrategy`（基于 `litellm`），五种策略：human / llm / react / verify / reflection。默认 llm，给定 instruction + 当前 agent 回复，吐下一句 user 话。
- **agent loop（参考实现）**：`tool_calling_agent.py` 一个简单的 OpenAI tool-calling 循环，最多 30 步，每步调一次 LLM、要么发 user message 要么发 tool call，全部 dispatch 给 `Env.step()`。
- **评分**：reward = 1.0 当且仅当 (a) DB 终态 hash 跟"replay ground-truth `actions`"产生的 hash 一致；(b) `outputs` 列表里每条字符串都在 agent 输出 trail 中出现过。否则 0.0。论文还给 `pass^k` (k 次独立采样里全部通过的 task 比例) 衡量稳健性。
- **规模**：retail 测试集 ~115 task，airline ~50 task。

### 1.2 不是什么

- **不是单轮 model bench**（vs MMLU / HumanEval）：每 task 5–30 turn，重在工具决策 + 多轮推理。
- **不是纯 schema function-call bench**（vs BFCL）：BFCL 只看一次"模型给的 tool call 名字 + 参数是否对"；τ-bench 看的是"在真实业务流程里 agent 能不能跑完任务，工具结果反馈正确利用，对话有没有偏离用户目标"。
- **不是 sandbox code benchmark**（vs SWE-bench）：没有 patch 提交、没有 pytest 验证；只有"业务终态 hash + 必答字段"。

---

## 2. 为什么贴 Agentloom

| # | τ-bench 需求 | Agentloom 现状对应 |
|---|---|---|
| 1 | 多轮 user ↔ agent 对话，turn 之间有持久状态 | ChatFlow 本来就是多轮树状对话，root → user_turn → agent_turn → … 完美贴合 |
| 2 | agent 每轮可以发多条 tool call + 1 条 user message | 内层 WorkFlow 本来就分 planner / worker / tool_call / draft，单 user turn 内允许多次 tool 调用与最终 agent_response |
| 3 | Tool catalog 在 turn 之间稳定可见、按需调用 | tool registry + `definitions_for_constraints` 已经在每次 LLM call 注入工具 schema |
| 4 | task 失败可以"reset 后重跑同一 instruction"做 pass^k | ChatFlow 复制（fork from root）或新建 chatflow + 同一 instruction 即可 |
| 5 | task 间彼此隔离，不串数据 | 每 task 起一个新 ChatFlow + 一份新的 mock DB；attach/detach 完成隔离 |
| 6 | 工具调用密集场景的稳定性优化（防 hallucinate "无权限"） | runtime envelope + auto_plan / native_react 模式选择已经做过 qwen36 / ark 的对比验证（`feedback_qwen36_tool_calls.md`）|

planner-driven decomposition（auto_plan 模式）对应 τ-bench 论文里讨论的"reasoning prefix"——这是一个潜在的 baseline 升级点，但**初版应该跑 `native_react` 模式**让 agent 行为最贴近 τ-bench 参考实现，方便横向对比。

---

## 3. 不贴的地方与 workaround

| Gap | 影响 | Workaround |
|---|---|---|
| τ-bench agent 是 SDK-style 同进程循环（`while not done: env.step(...)`），Agentloom 是 server + ChatFlow runtime | 不能直接 import `tau_bench` 的 agent 类塞进 Agentloom | runner 进程**不**复用 τ-bench 的 agent，只复用它的 `Env` + `UserStrategy` + `Task`；agent 角色由 Agentloom 后端通过 HTTP `/turns` 充当 |
| `Env.step(action)` 期望由 agent 喂 tool call，env 内部执行后返回 observation | Agentloom 的工具是 registry 在 engine 进程内本地执行，不会反喂回 τ-bench env | 把 τ-bench retail/airline 的 tool 类**包成 Agentloom `Tool` 子类**：execute 时直接 `tool_class.invoke(self._env_data, **args)`（τ-bench tools 本来就是无副作用的纯函数 + dict mutator），然后 reward 阶段 hash 这份共享 dict |
| `Env.calculate_reward()` 比 ground-truth replay，需要"原始未跑过的 db" + "agent 跑完之后的 db" | 必须保证 agent 的 tool 调用确实修改了 task 自己的 mock DB，不能跨 task 串 | runner 在每 task 起一个 fresh `Env`，把 `Env._data` 注入到一个**临时 ToolContext 字段**，τ-bench wrapper tool 都从 ctx 取这份 dict；task 结束 detach 时丢弃 |
| τ-bench tool catalog **per-domain** 动态切换（retail 一组、airline 一组），且不同 task 看到的 catalog 都不同（少数 task 限制工具子集） | Agentloom 现在 registry 是全局共享，跟 chatflow 无 1:1 绑定 | §5 给两个候选机制（A: register/unregister 临时全局；B: per-chatflow `extra_tools`）|
| τ-bench 用 `litellm`，依赖体积大，且 `openai>=1.13.3` / `anthropic>=0.26.1` 可能跟 Agentloom 后端的 SDK 钉住版本冲突 | pip install 一把就崩 | 单独 conda env `agentloom-bench`，runner 是单独 entry，不进 production server import 路径 |
| simulated user 是 LLM 调用，无法 deterministic 复现 | pass^k 指标本来就基于多次采样；但 1/0 比对在低 k 下波动可能掩盖 agent 改动效果 | 文档里钉死"baseline 跑 pass^1，optional 跑 pass^4"；runner 输出每次采样的 turn trace，方便人工 diff |

---

## 4. 集成架构

```
┌──────────────────────────────────────────────────────────────────────┐
│  agentloom_bench/  (独立 entry, 独立 conda env: agentloom-bench)     │
│                                                                       │
│   tau_bench_runner.py  ──── reads ──→  tau_bench.envs.retail.tasks    │
│        │                                                              │
│        │ for each task:                                               │
│        │   1. env = Env(task_index=i)                                 │
│        │   2. user = UserStrategy.LLM(instruction=task.instruction)   │
│        │   3. POST /api/tau-bench/sessions  (新 API, §5.A)            │
│        │      └──► backend creates fresh ChatFlow + injects tools     │
│        │   4. opening_msg = user.reset()                              │
│        │   5. loop:                                                   │
│        │       resp = POST /api/chatflows/{id}/turns {text: msg}      │
│        │       msg  = user.step(resp.agent_response)                  │
│        │       break if user signals "###STOP###" or max_turns        │
│        │   6. reward = env.calculate_reward()  ← 读 mock DB hash      │
│        │   7. POST /api/tau-bench/sessions/{id}/teardown              │
│        │   8. write per-task JSON to runs/{ts}/{task_idx}.json        │
│        │                                                              │
│   metrics_aggregator.py ──→  reads runs/{ts}/*.json                   │
│        └──► docs/reports/{ts}-tau-bench.md (gitignored, follow        │
│             feedback_agentloom_pr_sequencing.md 报告留痕约定)         │
└──────────────────────────────────────────────────────────────────────┘
                              ↕ HTTP
┌──────────────────────────────────────────────────────────────────────┐
│  Agentloom backend (production-style, 独立 conda env: agentloom)     │
│                                                                       │
│   POST /api/tau-bench/sessions   (新增, 仅 dev profile 暴露)          │
│      ├── creates ChatFlow with disabled_tool_names = [全部默认 tool]  │
│      │     → 让 agent 只看到 τ-bench 注入的 catalog                   │
│      ├── instantiates τ-bench wrapper tools, register 到 shared       │
│      │     ToolRegistry（命名空间 "tau__retail__get_order"）          │
│      └── 把 mock DB dict 绑到 ToolContext.tau_env_data 字段           │
│                                                                       │
│   POST /api/chatflows/{id}/turns   (现有)                             │
│      └── 走 ChatFlowEngine.submit_user_turn 正常路径                  │
│                                                                       │
│   POST /api/tau-bench/sessions/{id}/teardown                          │
│      ├── unregister τ-bench wrapper tools                             │
│      ├── engine.detach(chatflow_id)                                   │
│      └── 删除 chatflow 行（或留档供事后回看，按 flag）                │
└──────────────────────────────────────────────────────────────────────┘
```

关键不变量：

- **runner 跨进程，不 import agentloom**——这样 τ-bench 的 litellm 依赖与 Agentloom 后端隔离。
- **registry mutation 是临时的**：`add_source/remove_source` 已经验证过同类 lifecycle（`backend/agentloom/mcp/runtime.py:89-143`），照抄 pattern 不会引入新一致性风险。
- **mock DB 通过 ToolContext 注入**：避免在 `Tool.execute` 闭包里捕获跨 task 状态导致 leak；ctx 是 task-local，detach 时跟 chatflow 一起回收。

---

## 5. tool 注入机制：scoped tool registry

### 5.1 现状（grep 结论）

- `backend/agentloom/tools/registry.py:13-33` 的 `default_registry()` 是全局静态注册（Bash / Read / Write / Edit / Glob / Grep / get_node_context / memoryboard_lookup）。
- `ToolRegistry` 自身有 `register` / `unregister`（`backend/agentloom/tools/base.py:184-196`），duplicate 名字会抛 `ValueError`。
- MCP 已经在用动态 lifecycle（`backend/agentloom/mcp/runtime.py:89-143`：`add_source` register、`remove_source` unregister，每个 source 自己持有 `registered_names`）。
- Per-chatflow 隔离目前是**反向**的——只有 denylist `ChatFlow.disabled_tool_names`（`backend/agentloom/schemas/chatflow.py:353-364`）→ engine 在 `_filter_tool_defs` 里扣掉禁用 tool（`workflow_engine.py:1320-1324`）。**没有 per-chatflow `extra_tools` 字段。**

### 5.2 候选方案对比

#### 方案 A：临时全局 register/unregister（仿 MCP）

新增 `backend/agentloom/benchmarks/tau_bench/tool_source.py::TauBenchToolSource`：

- `connect_and_register(registry)` → 把 retail / airline 的工具一把注册进 shared registry，命名前缀 `tau_retail_` / `tau_airline_`。
- `close()` → 全部 unregister。
- 一个 chatflow 一份 source；session 创建时生成、teardown 时关闭。

每个 chatflow 在创建时把 **所有非 τ-bench 的工具**塞进 `disabled_tool_names`，确保 agent 只看到 τ-bench 这套；这样不影响其他正常 chatflow。

**Pros**：完全复用 MCP runtime 已有的 lifecycle，几乎零新基础设施；本机制本来就是 MCP 接入的天然 generalisation。
**Cons**：(a) 全局 registry 在多 task 并发跑时会**互踩**——retail task 0 注册 `tau_retail_get_order`，task 1 也想注册同名工具但绑不同的 mock DB。需要给每个 session 一个**唯一前缀**（如 `tau_{session_id}_get_order`），否则只能串行跑（其实串行更稳，论文 baseline 也基本串行）。(b) 全局 registry 暴露面变大，跟 capability model 哲学（per-node effective）逆向。

#### 方案 B：per-chatflow `extra_tools` 字段

`ChatFlow` 增 `extra_tool_definitions: list[dict]`（schema 里的纯定义），engine 在 `_filter_tool_defs` 处合并 effective registry + 当前 chatflow 的 extra；`Tool.execute` 走法对应改成"先查 chatflow-extra 再查 global"。

**Pros**：架构上更干净，跟 §M7.5 capability model 的 per-node 过滤同向；没有全局污染；多 task 真正可以并发。
**Cons**：需要改 ChatFlow schema + 持久化 + engine 的 tool dispatch 路径，**比 A 多 ~3 个 PR 体量**；M7.5 真做的时候还要再调。

#### 推荐：先 A 后 B

- **Milestone 1（本设计文档落地范围）**：用 A，session 唯一前缀 + 串行执行。够 ship，PR 少，不阻塞。
- **Milestone 2（M7.5 同期）**：迁到 B，作为 capability model "per-node tool 过滤"的衍生特性，τ-bench session 自然变成"创建一个 chatflow，capability bag 里只有 τ-bench tools"。
- **接口稳定锚**：runner 那侧只调 HTTP `/api/tau-bench/sessions`，方案 A → B 切换对 runner 透明。

---

## 6. simulated_user 实现

τ-bench 自己的 `UserStrategy.LLM`（`tau_bench/envs/user.py`）通过 litellm 调任意 provider，实现成熟。三个候选：

- **(a) runner 进程内直接调 τ-bench 的 `UserStrategy`**：simulated user 完全在 runner 一侧，Agentloom 收到的是普通 user_text turn，引擎无感知。
- **(b) Agentloom 内建一个"双 ChatFlow 互发"机制**：A ChatFlow 当 user，B ChatFlow 当 agent，两边 turn 桥接。
- **(c) simulated user 包成 tool**：agent 通过 `ask_user(...)` round-trip 拿 user 反馈。

**推荐 (a)**，理由：

1. 最贴近 τ-bench 原本设计，论文 baseline 跑出来的 trace 形态可以 1:1 比较。
2. 实现成本最低，不动 Agentloom 后端 turn 协议。
3. simulated user 的 LLM 调用花费走 runner 一侧的 provider key，跟 agent-side 调用计费分开记账。
4. 跟 §3 提到的 litellm 隔离一致——只在 runner conda env 装 litellm。

**user-side 模型选择**：按 `feedback_agentloom_provider_cost.md` 优先级，user-side 默认 `volcengine ark-code-latest`（免费档）；用户显式要求时切 `claude-sonnet-4-6` 复跑 ground truth。**注意**：论文 baseline 用 GPT-4，跟 ark 不可直接比，要在报告里标注。

**stop 信号**：τ-bench 的 user simulator 在判定任务完成时会发 `###STOP###`（见 `user.py`）；runner 看到此 token 就 break，触发 reward 计算。

---

## 7. 数据 / 评估接口：mock DB 绑到 Agentloom Tool 里

### 7.1 直接 import τ-bench 工具实现（推荐）

```python
# agentloom_bench/tau_bench_adapter.py
from tau_bench.envs.retail.env import MockRetailDomainEnv
from tau_bench.envs.retail.tools import ALL_TOOLS as RETAIL_TOOLS

class TauBenchToolWrapper(agentloom.tools.Tool):
    def __init__(self, tau_tool_cls, env_ref):
        self._impl = tau_tool_cls
        self._env = env_ref      # 同一 task 内共享的 mock dict
        self.name = f"tau_{session_id}_{tau_tool_cls.__name__}"
        self.description = self._impl.get_info()["function"]["description"]
        self.parameters  = self._impl.get_info()["function"]["parameters"]

    async def execute(self, args, ctx):
        # tau_tool 本来都是同步函数，run_in_executor 包一下
        out = await asyncio.to_thread(self._impl.invoke, self._env.data, **args)
        return ToolResult(content=str(out), is_error=False)
```

**优点**：tau_bench 工具实现是 reward 比对的真值来源，复制一份会很快漂移；直接 import 就保证一致。
**缺点**：需要双 conda env（runner 装 tau_bench；后端**也**得装 tau_bench wheel 才能 import 它的 tool 类）。这是本设计最大的依赖洁癖代价。

#### 代价分摊办法：dev-only extras

`backend/pyproject.toml` 的 `[project.optional-dependencies]` 加一个 `tau-bench` extra：`pip install -e ".[tau-bench]"`。生产部署不装，dev 跑 benchmark 时才装。冲突风险在第一个 PR 的 smoke test 里就暴露——验证策略见 §11。

### 7.2 复制工具逻辑（不推荐）

会引入 ~50+ 函数的二次实现 + 持续 maintain 它们跟上游同步的负担，第一次 ship 没必要。

### 7.3 ToolContext 扩展

`ToolContext` 增 optional 字段 `tau_env: TauBenchEnvHandle | None`（默认 `None`，正常 chatflow 不受影响）；engine 在创建 ToolContext 时按 `chatflow.tau_session_id` 查表注入。`TauBenchToolWrapper.execute` 优先从 ctx 取，fallback 到构造时绑定的 env_ref——保留两条路是为了让单元测试能 bypass HTTP 直接 invoke wrapper。

---

## 8. runner 实现位置

```
agentloom_bench/                      # 仓库根新建（独立 distribution，跟 backend 同 monorepo）
├── pyproject.toml                    # 自己一份 deps：tau_bench, httpx, typer
├── README.md                         # 跑法 + 限制
└── src/agentloom_bench/
    ├── __init__.py
    ├── tau_bench/
    │   ├── runner.py                 # CLI entry: `python -m agentloom_bench.tau_bench`
    │   ├── client.py                 # httpx.AsyncClient 封装 backend HTTP
    │   ├── tool_source.py            # TauBenchToolSource (方案 A)
    │   ├── adapter.py                # TauBenchToolWrapper
    │   └── report.py                 # JSON → markdown 聚合
    └── tests/
        └── smoke/test_import.py
```

CLI：

```
python -m agentloom_bench.tau_bench \
    --domain retail \
    --task-ids 0-9 \
    --backend-url http://localhost:8000 \
    --agent-model ark-code-latest \
    --user-model ark-code-latest \
    --user-strategy llm \
    --max-turns 30 \
    --out runs/2026-04-25-retail-0to9
```

每 task 输出 `runs/.../task_{idx}.json`，含：

```json
{
  "task_idx": 0,
  "domain": "retail",
  "instruction": "...",
  "agent_model": "ark-code-latest",
  "user_model": "ark-code-latest",
  "turns": [{"role": "user", "text": "..."}, {"role": "agent", "text": "...", "tool_calls": [...]}, ...],
  "reward": 0.0,
  "reward_breakdown": {"db_hash_match": false, "outputs_match": true},
  "turn_count": 12,
  "tool_call_count": 7,
  "duration_seconds": 281.4,
  "stop_reason": "user_stop_token | max_turns | error"
}
```

---

## 9. 第一次跑的 minimum viable scope

- **Domain**：retail（fact pattern 比 airline 简单：订单 / 退款 / 改地址，不涉及多段航班行程）。
- **Task 范围**：0–9（10 个），覆盖退款 / 取消 / 修改地址 / 加商品几类典型动作。
- **Token 预算**：
  - 每 turn 估 ~3K input + ~1K output（retail 工具 schema ~1.5K，对话历史 1–2K，agent 响应短）。
  - 每 task 5–15 turn × 4K tokens × 2（in+out 都计）= 40–120K tokens / task × 10 task = **400K–1.2M tokens**。
  - ark-code-latest 免费包月（`feedback_agentloom_provider_cost.md`：volcengine packaged，1200 req/5h），10 task 大概 100–300 req，跑得起。
- **时间预算**：每 turn ark ~30s（含 tool exec 1s 内）→ 每 task 2.5–7.5 min × 10 = **30–75 min**。runner 要打 progress log 而不是裸 wait。

---

## 10. 指标输出

每跑一批，runner 写一份 `docs/reports/{ts}-tau-bench-{domain}-{range}.md`（gitignored），包含：

| 类目 | 指标 |
|---|---|
| 总分 | pass^1 / per-task pass-fail 表 |
| 软指标 | 出现率 = `outputs` 命中 / 总 outputs；DB hash 命中率独立列 |
| 行为分布 | tool_call count 直方图 / turn count 直方图 |
| 错误分类（人工 + 自动） | `agent_giveup` / `wrong_tool_name` / `wrong_arg` / `hallucinated_answer` / `loop_no_progress` / `user_simulator_misread` |
| 对照基线 | 论文里 GPT-4 / Claude-3.5 retail 数据（pass^1 ≈ 50%）；本次 ark / qwen 跑出的数 |
| 模型差异 | 同 instruction 在 ark / qwen36 / claude 三模型下 reward 矩阵（可选 follow-up）|

错误分类前两版用人工标 + GPT-4 batch judge 各做一次互校；自动判定逻辑放 `report.py::classify_failure(turns)`，规则简单（看最后一条 agent_text、tool_calls 序列、reward.breakdown）。

---

## 11. PR 拆分（小步策略）

| PR | 内容 | 估行数 | 验证 |
|---|---|---|---|
| **PR 1** | Vendoring（v0.2 修订）：`vendor/tau_bench/` 子集（仅 `envs/{retail,airline}/` + `types.py` + `LICENSE` + 顶层 `__init__.py`），backend `pyproject.toml` 加路径 fixture 让 vendor 可 import；`agentloom_bench/` 仓库骨架 + 自己的 conda env 装完整上游 tau_bench；smoke test 双向验证 | ~600（含 vendor data） | (a) backend 内 `from tau_bench.envs.retail.env import MockRetailDomainEnv` 不抛；(b) `agentloom-bench` env 内 `from tau_bench.envs.user import UserStrategy` 不抛；(c) 两 env 独立装的 sha 一致 |
| **PR 2** | Scoped tool registry（方案 A）：新 API `POST /api/tau-bench/sessions` + `tool_source.py` + `TauBenchToolWrapper`（仅 retail）+ `ToolContext.tau_env` 字段 + 单元测试（不连真 LLM，用 stub provider 跑一个工具调用 round trip） | ~600 | 单元测试：`test_tau_session_register_unregister` / `test_tau_tool_invokes_mock_db_and_mutates` |
| **PR 3** | simulated_user driver：runner 端 `client.py`（httpx 封装 turns API）+ `runner.py` 单 task 模式骨架，能调 τ-bench `UserStrategy` 与 backend 串成 loop。先用本地 stub backend 跑 | ~500 | smoke test：把 backend 起在 `localhost:18000`，用 echo provider 跑 retail task 0 跑通 5 个 turn 不报错 |
| **PR 4** | runner CLI 完整 + reward 接入：单个 task 端到端能跑通（真 ark），写出 task_0.json 含 reward | ~400 | 实跑：retail task 0 用 ark-code-latest，输出 reward 不抛、turn trace 完整 |
| **PR 5** | 多 task batch + report aggregation：跑 retail 0-9，写 `docs/reports/{ts}-tau-bench-retail-0to9.md` | ~400 | 实跑 10 task；报告生成正确；按 §10 各列 |
| **PR 6** | airline domain 复用 + CI 周期任务（GitHub Actions weekly cron 跑 5 个 sample task，结果存 artifact）| ~400 | CI 绿；weekly artifact retention 4 周 |

PR 1 / 2 / 3 互相**有强依赖**：PR 2 依赖 PR 1 的 import 通；PR 3 不强依赖 PR 2，可以并行起草，但 merge 顺序 1→2→3。PR 4 必须 1+2+3 都在。

---

## 12. 风险

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| τ-bench 仓库 `litellm` 依赖跟 Agentloom 后端 SDK 钉版本冲突 | 高 | 装不上 | 双 conda env 隔离；后端只装 tau-bench extra 时小心 pin；PR 1 smoke 暴露 |
| `tau_bench` 上游不打 wheel / 不上 PyPI（直接 GitHub install） | 中 | pip 解析慢 + CI 不稳 | 用 git+ssh URL pinned commit；本地 mirror tarball 兜底 |
| user-side 模型用 ark 跟论文 GPT-4 不可直接比 | 必现 | 数字本身不能直接对标论文 | 报告标注；用户要求时跑 claude-sonnet 当 user 复跑 |
| simulated user 不发 `###STOP###`（弱模型 / agent 跑飞），打满 max_turns 浪费 | 中 | token 预算 over | runner 强制 max_turns=30，超时算 fail；且加 "no-progress detector"（连续 3 turn 同样 tool 同样参数）|
| reward 计算依赖 mock DB 终态 hash；如果 wrapper tool 没把 mutation 写回同一份 dict，reward 永远 0 | 高（实现 bug 易踩） | 全部 task 永远 fail | 单元测试钉死："wrapper tool execute 后 env._data 被改"；并跟 τ-bench 自己 agent 跑一遍 ground-truth replay 做 sanity（reward 应该全 1）|
| Agentloom 当前的 `_assert_frozen_chatflow_nodes_unchanged` 在 τ-bench 跨 turn 触发 race（`project_agentloom_complex_e2e_2026_04_26.md`）| 低（commit `a056243` 已修）| HTTP 500 | 复现可能性低；runner 串行跑同一 chatflow 默认满足 lock 模型 |
| ChatFlow 历史长 → token 暴涨 → ark 上下文窗口爆 | 中 | task 跑到一半截断 | retail task 平均 turn ≤ 15，ark 131K context 够；airline 长 task 给 chatflow 默认 `compact_trigger_pct=0.7` 让自动压缩兜底 |
| qwen36 拒调工具 hallucinate（`feedback_qwen36_tool_calls.md`）| 高（如果用 qwen） | reward 全 0 | baseline 默认 ark；qwen36 跑只作为对照实验，不当默认 |

---

## 13. 风险与权衡（设计决定级，可能反悔）

- **tool registry 走方案 A 还是直接做 B**：选 A 是赌"M7.5 之前先 ship 一版 benchmark 数据"。如果 M7.5 比预期快，可能 PR 5 之前就要切到 B，导致重写 `tool_source.py`——届时把 wrapper tool 类抽到稳定接口能降低重写成本，runner 一侧不变。
- **runner 是不是该融进 backend 的 `scripts/`**：本设计选独立 distribution（`agentloom_bench/`），是为了 deps 隔离。代价是仓库多一个 package。如果用户要单仓 monorepo 风格，可以改成 `backend/scripts/benchmarks/tau_bench/`，但需要在该目录 README 显式标记"装 tau-bench extra 才能跑，否则 import 会炸"——经验上 dev 一定会忘记装，宁可多一层目录。
- **走 HTTP 还是直接 in-process 调 ChatFlowEngine**：选 HTTP 是为了 (a) 模拟真实 latency / 失败模式；(b) 跟 BFCL / SWE-bench 未来复用同一个 driver pattern；(c) runner / backend 进程隔离，崩一个不影响另一个。代价是 PR 3 多一份 httpx wiring。
- **本里程碑 vs 后续 BFCL / SWE-bench 复用**：runner 抽象层应该是 `BenchmarkRunner(driver, env, user, eval)`，τ-bench 是其中一个 `(env, user, eval)` 实例。BFCL 没有 user simulator + 没有多 turn，会退化为 `env=None, user=None`；SWE-bench 没有 user 但有更复杂的 patch eval。**第一次写 τ-bench 时不强行抽象**——先把 τ-bench 跑通，第二个 benchmark 加进来时再提取共性，避免 premature abstraction。

---

## 14. 第一步行动（PR 1 落地清单）

| 文件 | 改动 | 估行数 |
|---|---|---|
| `vendor/tau_bench/{envs,types.py,LICENSE,__init__.py}` | 从 sierra-research/tau-bench 上游 cherry-pick envs 子集 + types + LICENSE，**不**带 agents/ 和 user.py 进 vendor | +500 (含 retail/airline JSON data) |
| `vendor/README.md` | 写 vendor 来源 sha + 同步规程 + 不修改原则（patch 须经 review） | +30 |
| `backend/pyproject.toml` | 加 vendor 路径到 setuptools `packages` / 或加 `tool.uv.sources`，让 backend env 装上 agentloom 时同时装上 vendor 子集（不再 `git+`） | +10 |
| `agentloom_bench/pyproject.toml` | 新建：name, deps (`tau_bench @ git+https://...@<sha>`, httpx, typer, pytest)；这里**装完整上游**含 user.py + litellm | +30 |
| `agentloom_bench/src/agentloom_bench/__init__.py` | 空 | +1 |
| `agentloom_bench/src/agentloom_bench/tau_bench/__init__.py` | 空 | +1 |
| `agentloom_bench/tests/smoke/test_import.py` | smoke test：import + reset retail task 0 | +30 |
| `agentloom_bench/README.md` | 跑法 + double-env 说明 + 当前 limit（仅 retail / 仅 ark / pass^1） | +60 |
| `docs/design-tau-bench-integration.md` | 本设计文档 | ~600 |

验证脚本：

```bash
conda create -n agentloom-bench python=3.13 -y
conda activate agentloom-bench
cd agentloom_bench && pip install -e .
pytest tests/smoke -q     # 应该 1 passed
```

如果 tau_bench 上游 setup 有问题，PR 1 直接卡住，触发 fallback：fork 到 `usingnamespacestc/tau-bench` + pin sha 自管。

---

## 15. 落地回顾摘要

- **(a) 集成方案核心是什么（一句话）**：runner 走 HTTP 把 τ-bench 的 `Env` + `UserStrategy` 跟 Agentloom 的 ChatFlow 串成 multi-turn loop，τ-bench retail / airline 工具临时注册到 Agentloom tool registry，task 跑完直接调 τ-bench 自己的 `calculate_reward()` 拿分。
- **(b) PR 总数与第一个 PR 估行数**：6 个 PR；PR 1 ≈ 130 行（不含 design 文档）。
- **(c) v0.2 拍板（2026-04-26）**：
  1. tau_bench 来源：**B 子集 vendor**——只 vendor `envs/` + `types.py` + `LICENSE` 进 `vendor/tau_bench/`，backend 零依赖污染；runner conda env 单独装完整上游。
  2. user-side LLM：**ark-code-latest**——免费档先跑全套，论文 GPT-4 baseline 不可直接比但能做 Agentloom 自己的回归追踪；后续 sonnet 复跑作为对照。
  3. runner 位置：**独立 `agentloom_bench/` distribution**——保持 deps 隔离，conda env 用 `agentloom-bench`，跟 backend `agentloom` 解耦。
- **(d) 跟 M7.5 / capability model 的关系**：**可以并行，不需要等 M7.5**。本里程碑用方案 A（临时全局 register/unregister）；M7.5 落地时把 τ-bench 的 tool source 迁到 per-chatflow capability bag（方案 B）。两者切换对 runner / 测试 / 报告全透明，只动 backend 内部 `tool_source.py` 一处。M7.5 不会因为 τ-bench 改变排期，τ-bench 也不会因为等 M7.5 推迟。

---

## 16. 关键文件索引

实施时改动会聚焦在这些文件：

- `backend/agentloom/tools/base.py` — Tool ABC + ToolRegistry，τ-bench wrapper 必须遵循的接口
- `backend/agentloom/mcp/runtime.py` — register/unregister lifecycle 的参考实现，scoped tool source 直接抄此 pattern
- `backend/agentloom/engine/chatflow_engine.py` — `submit_user_turn` + attach/detach + `_submit_locks` 是 runner 串 turn 的所有 entry
- `backend/agentloom/api/chatflows.py` — `POST /turns` 现有协议；新 `POST /api/tau-bench/sessions` 仿此 pattern 实现
- `backend/agentloom/schemas/chatflow.py` — `ChatFlow.disabled_tool_names` 用来"屏蔽默认工具只露 τ-bench 一组"的字段
