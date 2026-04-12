# Agentloom 开发日志

本文件记录后端 MVP 的实际开发过程、遇到的坑、以及每一步的决策。按时间顺序从上往下写，每个小节对应一个会话轮次或一个里程碑。测试数字都是实跑过的。

`docs/` 目录在 `.gitignore` 中（见根 `.gitignore` 的 `/docs/`），所以本日志和其它设计文档一样只留本地，不入 git。

---

## 第 1 轮 — M0 仓库脚手架

搭 monorepo 骨架：

- `backend/` — Python 3.12 + FastAPI + SQLAlchemy 2.x async + Pydantic v2
- `frontend/` — 占位（未开工）
- `tests/backend/{unit,integration,smoke,fixtures}/` — 四层测试分层
- conda 环境 `agentloom`（记忆：base 环境不装任何东西）
- `backend/pyproject.toml` + `pytest.ini` 设置 `asyncio_mode = "auto"`
- `alembic/` 迁移骨架 + `env.py`

顺带把 `docs/requirements.md`、`docs/plan.md`、`docs/testing.md` 三份主干文档先写了。

---

## 第 2 轮 — M1 数据模型

Pydantic schemas 作为唯一真相源，SQLAlchemy 只负责持久化（payload 存 JSONB）：

- `schemas/common.py` — `NodeStatus`（planned/running/waiting_for_rate_limit/succeeded/failed/retrying/cancelled，`is_frozen` 属性）、`StepKind`、`EditProvenance`、`EditableText`、`ToolConstraints`、`NodeBase`、`ToolUse`/`ToolResult`、`TokenUsage`
- `schemas/chatflow.py` — `ChatFlow`、`ChatFlowNode`（外层对话节点）
- `schemas/workflow.py` — `WorkFlow`、`WorkFlowNode`（内层 DAG 节点，支持 `llm_call` / `tool_call` / `sub_agent_delegation`）
- `schemas/provider.py` — `ProviderKind`、`ProviderConfig`、`ModelInfo`
- `db/models/` — 每张表一个 ORM 模型：`workspaces` / `users` / `providers` / `chatflows` / `chatflow_shares` / `workflows` / `workflow_templates` / `channel_bindings` / `dashed_node_locks` / `audit_log`
- **每个 user-scoped 行都有 `workspace_id` + nullable `owner_id`**（ADR-015 / ADR-017）
- `alembic/versions/0001_initial.py` — 一次性建全部表，并 seed `workspaces` 单行 `'default'`

关键不变量（在 schema 测试里断言）：冻结节点（succeeded/failed）不可修改——`NodeBase.require_mutable()` 在每个 mutation 路径顶上调用。

---

## 第 3 轮 — M2 OpenAI-compat + Volcengine 真机烟测

`backend/agentloom/providers/` 的第一个适配器：

- `providers/types.py` — `Message`（`SystemMessage`/`UserMessage`/`AssistantMessage`/`ToolMessage`，带 `cache_breakpoint` 字段为 M5 预留）、`ChatResponse`、`TokenUsage`、`ToolDefinition`、`FinishReason` Literal
- `providers/base.py` — `ProviderAdapter` ABC + `ProviderError`
- `providers/openai_compat.py` — `OpenAICompatAdapter`，`httpx.AsyncClient` 走 `/v1/chat/completions`，支持 tools、指数退避重试 5xx/429、ChatResponse 解包
- `providers/registry.py` — `build_adapter()` 工厂
- `tests/backend/unit/test_providers_openai_compat.py` — 9 个单测，用 `httpx.MockTransport` 伪造返回值

**真机烟测：**
- `tests/backend/smoke/test_volcengine_live.py` — gated 在 `VOLCENGINE_API_KEY` + `AGENTLOOM_RUN_LIVE_SMOKE=1`，打 `https://ark.cn-beijing.volces.com/api/coding/v3` 的 `ark-code-latest`，验证 hello world + 工具调用两个场景
- 跑通过，确认 wire 格式正确

---

## 第 4 轮 — M11 early: 分层 Token Bucket（HTB）

提前做限流是因为真机烟测时差点把 Tavily 的免费配额打穿。

- `backend/agentloom/rate_limit/htb.py` — 分层 token bucket：根节点是全局限制，叶子节点是单 provider/单工具的 QPS 限制。节点继承父节点的 token 消耗。
- 9 个单测覆盖：原子消费、层级穿透、空 bucket 阻塞+唤醒、异步并发安全
- 配置实例：Volcengine 10 QPS、Tavily 1 QPS

此时把记忆里「VOLCENGINE_API_KEY 和 TAVILY_API_KEY 都可用」固化下来。

---

## 第 5 轮 — M3 WorkFlow 引擎 + Postgres + FastAPI + SSE

**后端 MVP 的核心**，此轮耗时最长，遇到两个独立的 pytest 阻塞。

### 实现

- `backend/agentloom/engine/workflow_engine.py` — Kahn 拓扑排序执行器：
  - `execute(workflow)` 跑完所有 `PLANNED` 节点到 `SUCCEEDED`/`FAILED`
  - `_run_llm_call(node)` 构 context（沿祖先链回溯），调 provider，写回 `llm_response`
  - 失败节点冻结为 `FAILED` + error message
- `backend/agentloom/engine/events.py` — `EventBus` + `WorkFlowEvent`（`kind`/`chatflow_id`/`node_id`/`status`/`payload`），进程内 `asyncio.Queue` 实现
- `backend/agentloom/db/repositories/workflow.py` — `WorkflowRepository`：workspace-scoped create/get/save/list_ids；**save 时做 frozen node deep-diff**，任何改动已冻结节点的字段都抛 `FrozenNodeError`
- `backend/agentloom/api/workflows.py` — REST：POST 创建、GET 读取、POST `/execute` 触发、GET `/events` SSE
- `tests/backend/unit/test_repo_hygiene.py` — **AST 扫描测试**：遍历 repository 源码，强制每个 `select()` 调用必须有 `.where(... workspace_id ...)`。保证 ADR-015 的跨 workspace 隔离合规。

### 坑 1：pytest-asyncio STRICT vs AUTO

`rootdir` 解析到 `/home/usingnamespacestc/Agentloom` 而不是 `backend/`，所以 `backend/pyproject.toml` 里的 `asyncio_mode = "auto"` 根本没被读到，测试全部以 STRICT 模式收集，异步 fixture 直接报错。

- 尝试 1：`pytest_collection_modifyitems` hook 改 mode — **失败**，pytest-asyncio 在 collection 之前就决定了模式
- 尝试 2：`pytest_configure(config)` 里 `config.option.asyncio_mode = "auto"` — **成功**

改在 `tests/backend/integration/conftest.py` 里。

### 坑 2：集成测试挂死

连单个 test 都会卡住。先怀疑 SQLAlchemy 连接池，换成 `NullPool` 每个 test 都新建连接——**没解决**。用裸 Python 复现出同样的挂死，确认是代码 bug 不是 pytest。

**根因：** `test_events_are_emitted_in_order` 里有一个异步 race。测试代码写成：

```python
collector_task = asyncio.create_task(collect())
await engine.execute(wf)        # 同步跑完所有节点，每个发 bus.publish()
events = await collector_task   # 永远等不到
```

问题是 `create_task` 只是调度了 `collect()`，没让它真正 run。`stub_provider` 是纯同步返回 `ChatResponse`，`bus.publish()` 在 0 订阅者时也没有 await 点，所以 `execute()` 一口气跑完所有 publish 后才轮到 `collect()` 去 subscribe——订阅者晚到，事件已经发完了。

**修复：** `await asyncio.sleep(0)` 紧跟 `create_task` 之后，把控制权让给 collector 先完成 subscribe。

### M3 成果

17 个 repository + engine + SSE 集成测全绿。

---

## 第 6 轮 — M4 ChatFlow + channel adapter hook + FakeAdapter

外层对话层 + 把 ChatFlow 和外部 IM 绑起来的插件点：

- `backend/agentloom/engine/chatflow_engine.py` — `ChatFlowEngine`：
  - `submit_user_turn(chat, text, parent_id=None)` 追加 user turn → 沿祖先链建 context → seed 内层 WorkFlow 一个 `llm_call` 节点 → 委托给内层 `WorkflowEngine` → 冻结 agent_response 为 `ChatFlowNode`
  - `on_external_turn(chatflow, turn)` — ADR-016 的外部 channel 桥接，返回纯文本答复
- `backend/agentloom/db/repositories/chatflow.py` — 镜像 `WorkflowRepository`，同样的 frozen 检查，同样的 workspace scope。`_assert_frozen_chatflow_nodes_unchanged` 对外层 `ChatFlowNode` 和内层 `WorkFlowNode` 都做 deep-diff
- `backend/agentloom/channels/base.py` — `ChannelAdapter` ABC（`start`/`stop`/`send`）+ `ExternalTurn` dataclass + `FakeAdapter`（`inject(turn)` 给测试用，`sent: list[str]` + `started: bool`）
- `backend/agentloom/api/chatflows.py` — REST：POST 创建、GET 读取、POST `/turns` 提交、GET `/events` SSE

### 坑 3：monkeypatch 打不到已绑定的名字

`test_create_turn_and_get` 一直失败。`chatflows.py` 最初写的是：

```python
from agentloom.api.workflows import _provider_call_from_settings
```

这把名字在模块加载时就绑死了。测试 monkeypatch `workflows._provider_call_from_settings` 根本影响不到已绑定的本地引用。

**修复：** 改成 `from agentloom.api import workflows as _workflows_api`，路由 handler 每次调用时走 `_workflows_api._provider_call_from_settings()`。这样 monkeypatch 就能生效。

---

## 第 7 轮 — M6 工具执行（Bash/Read/Write/Edit/Glob/Grep）

**用户明确指定 M6 要优先于 M5 做**，因为没有工具执行，Anthropic 的 cache_control 也没东西缓存。

### 实现

- `backend/agentloom/tools/base.py`：
  - `Tool` ABC：`name` / `description` / `parameters` / `execute(args, ctx)` / `definition()` / `detail_for_constraints(args)`
  - `ToolContext` dataclass：`workspace_id` / `cwd` / `env`
  - `ToolError` 异常
  - `ToolRegistry`：`register` / `get` / `all` / `definitions_for_constraints` / `check_call` / `execute`
  - **约束语法**：正则 `^(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:\((?P<detail>.*)\))?$`，detail 走 `fnmatch`。支持 `Bash`、`Bash(git *)`、`Read(/tmp/*)` 等形式
- `backend/agentloom/tools/bash.py` — `asyncio.create_subprocess_shell`，默认 30s 超时上限 600s
- `backend/agentloom/tools/files.py` — `ReadTool`（offset/limit 窗口 + 行号前缀）、`WriteTool`（自动创建父目录）、`EditTool`（`replace_all=false` 时要求 old_string 唯一，`old==new` 拒绝）
- `backend/agentloom/tools/search.py` — `GlobTool`（按 mtime 倒序，上限 250）、`GrepTool`（支持 `glob` 过滤 + `case_insensitive`）
- `backend/agentloom/tools/registry.py` — `default_registry()` 工厂

### 引擎侧改造

`workflow_engine.py` 同步改造，支持工具循环：

- 构造函数加 `tool_registry` + `tool_context` 参数
- `MAX_TOOL_LOOP_ITERATIONS = 12`
- `execute()` 改写成**循环直到无进展**，处理工具循环中动态新增的节点
- 新增 `_run_tool_call()` — 调 `self._tools.execute(...)` 并写 `node.tool_result`
- 新增 `_spawn_tool_loop_children()` — 当 llm emits `tool_uses` 时，为每个 tool_use 生成一个 `tool_call` 子节点，再加一个 follow-up `llm_call` 收敛
- 新增 `_assert_tool_loop_budget()` — 沿祖先链数 `llm_call` 节点，≥12 时 FAILED + "budget"
- `_build_context_from_ancestors()` 增加对 frozen `tool_call` 节点的处理：用 `source_tool_use_id` + `tool_result.content` 拼 `ToolMessage`

### 坑 4：约束拒绝没有统一 is_error=true

3 个测试失败：`test_constraint_deny_blocks_execute`、`test_constraint_allow_rejects_non_matching_detail`、`test_tool_loop_honors_constraints`。

**根因：** `ToolRegistry.execute()` 把 `self.check_call(...)` 放在 try/except **外面**，导致约束违规直接抛 `ToolError`，而运行时工具错误却被捕获成 `ToolResult(is_error=True)`——行为不统一，引擎的 `tool_call` 节点被标 FAILED 且没设 `tool_result`。

**修复：** 把 `check_call` 挪进 try 块。这样约束拒绝和工具运行时错误都返回统一的 `ToolResult(is_error=True, content=...)`，LLM 下一轮能看到错误文本并自我纠正。

### M6 成果

- 21 个工具单测
- 3 个 `test_tool_loop.py` 集成测：scripted provider 返回 tool_use → 引擎跑工具 → follow-up llm_call 能看到 ToolMessage；约束拒绝路径；预算耗尽路径

---

## 第 8 轮（本轮上半）— M5 Anthropic 原生适配器 + cache_control

**为什么不塞进 OpenAI compat：** Anthropic 的三个结构差异任何一个都无法在 compat 壳子里表达——`cache_control` 需要 per-block 标记、消息是 block 数组（text/tool_use/tool_result）、`system` 是顶层字段而不是消息。prefix cache 在长工具循环里是 ~5× 成本优化，不做这个 M5 就白做。

### 实现

`backend/agentloom/providers/anthropic_native.py`：

- 构造函数：`cache_system=True` + `cache_last_user=True` + `extra_headers` + `timeout=120.0`
- `_split_system()` — 抽出所有 `SystemMessage` 拼成顶层 `system` block 列表，最后一块打 `cache_control`；调用方可以通过 `cache_breakpoint=True` 强制打标
- `_to_wire_messages()` — 核心转换：
  - `UserMessage` → `{"role": "user", "content": [{"type": "text", ...}]}`
  - `AssistantMessage` → text block + tool_use block 顺序输出
  - 连续 `ToolMessage` → 合并成单个 user turn 的 `tool_result` block 列表
  - 最后一个 user turn 的末尾 block 打 `cache_control`
- `_to_wire_tools()` — 字段名是 `input_schema` 不是 `parameters`
- `_parse_response()` — 读 content blocks 映射到 `ToolUse`，`stop_reason` 映射到 `FinishReason`，`cache_read_input_tokens` 透出为 `cached_tokens`，`prompt_tokens = input + cache_creation + cache_read`
- `chat()` — `max_tokens` 默认 4096（Anthropic 必填），POST `/v1/messages`，5xx/429 指数退避
- `list_models()` — 返回硬编码 `["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]`
- `providers/__init__.py` + `providers/registry.py` 把 `AnthropicNativeAdapter` 注册进 `_KINDS`

### 测试

`tests/backend/unit/test_providers_anthropic_native.py` — 16 个单测：
- wire shape: `_split_system`、`_to_wire_messages` 各种组合（user/assistant/tool/多 system/连续 tool）
- cache 位置：system 最后一块、user turn 最后一块、`cache_system=False` 时不打标、显式 `cache_breakpoint` 覆盖
- 顺序保持：ADR-013 断言 u1/a1/u2/a2/u3 顺序不变
- `_to_wire_tools` 字段名是 `input_schema`
- `_parse_response` 基本 text、tool_use、cache 计数
- HTTP：headers 验证（`x-api-key` + `anthropic-version: 2023-06-01` + 小写 `content-type`）、5xx 重试、4xx 抛 `ProviderError`

**16/16 全绿。** 单靠单测验证，没跑真机（没配 `ANTHROPIC_API_KEY`，且国内网络是额外问题——这也是用户最终还是选飞书 + 国产模型的动机之一）。

---

## 第 9 轮（本轮下半）— M7 MCP 客户端 + Tavily 真机烟测

### 调研

先 probe MCP Python SDK 表面：

```
mcp.ClientSession — async context manager
mcp.StdioServerParameters + stdio_client — subprocess transport
mcp.client.streamable_http.streamable_http_client — 新 API（headers/timeout 走 http_client 工厂）
mcp.client.streamable_http.streamablehttp_client — 旧 API 有 DeprecationWarning
mcp.shared.memory.create_connected_server_and_client_session — 内存 client+server 对
mcp.server.lowlevel.Server — 低层 server，用 @list_tools() / @call_tool() decorator
mcp.types.{Tool, CallToolResult, TextContent}
```

关键发现：SDK 内置 `create_connected_server_and_client_session`，能在进程内跑一个真 MCP server + client 对——意味着单测可以跑**真协议**而不是手搓 mock。

### 实现

`backend/agentloom/mcp/`：

- `types.py` — `MCPServerKind`（http / stdio）+ `MCPServerConfig`。三条校验：
  - http 必须有 `url`
  - stdio 必须有 `command`
  - `server_id` 必须是 identifier-safe `[A-Za-z0-9_]+`（因为要嵌进工具名给 LLM 看）
- `client.py` — `MCPClient` 包装器：
  - `AsyncExitStack` 把 `streamable_http_client` + `ClientSession` 两层 async context 压平成一个 `connect()` / `close()` 对
  - http 分支：`create_mcp_http_client(headers, timeout)` 做工厂，owned by stack
  - stdio 分支：`stdio_client(StdioServerParameters(...))`
  - `list_tools()` → `list[mcp_types.Tool]`
  - `call_tool(name, arguments)` → `(text, is_error)`，content blocks 里 text 拼接、非 text 用 `[<kind> block]` 占位
  - 所有 SDK 异常转 `MCPClientError`
- `tool_adapter.py` — `MCPRemoteTool(Tool)`：
  - `name = "mcp__<server_id>__<sanitized_tool_name>"`，非 `[A-Za-z0-9_]` 字符替换成下划线——**必须**，因为 `ToolConstraints` 正则只接受 identifier 字符（踩过坑：Tavily 真实的 tool 名叫 `tavily-search` 带横线）
  - `detail_for_constraints` 返回 `str(args)` 供 glob 匹配整个 payload
  - `execute()` 调回 client，`MCPClientError` 转 `ToolError`
- `bridge.py` — `MCPToolSource`：
  - 持有一个 `MCPClient` + 注册到 `ToolRegistry` 的工具名列表
  - `connect_and_register(registry)` — 幂等，connect → list → 批量包装成 `MCPRemoteTool` 注册进 registry
  - `close()` — 关 client
- `__init__.py` — 公开 API

### 坑 5：deprecation warning

第一次真机烟测跑通后，pytest 报 `DeprecationWarning: Use streamable_http_client instead`。旧的 `streamablehttp_client` 直接接受 `headers` + `timeout`，新的 `streamable_http_client` 只接受 `http_client`。

**修复：** 用 `mcp.shared._httpx_utils.create_mcp_http_client(headers, timeout)` 工厂预先建 `httpx.AsyncClient`，再传给 `streamable_http_client(url=..., http_client=...)`。`http_client` 也 enter 进 `AsyncExitStack`，close 时统一拆。

### 测试

`tests/backend/unit/test_mcp_client.py` — 15 个单测，用 `create_connected_server_and_client_session` + 内存 `Server`（2 个工具：`echo`、`fail`），跑**真 MCP 协议**：

- 3 个 config 校验（http 缺 url、stdio 缺 command、server_id 非法字符、cross-field 矛盾）
- 1 个命名 sanitization（`tavily_search` / `repo-list` / `v1.read`）
- 5 个 `MCPClient` 协议测：list、call 成功、call 失败返回 `is_error=True`、未 connect 抛 error
- 2 个 `MCPRemoteTool` 包装：`ToolResult` 往返 + 错误处理
- 4 个 `MCPToolSource` 桥接：批量注册、通过 registry 调用、deny 约束生效、close 状态
- 1 个 `ToolError` 未连接路径

因为 `MCPClient.connect()` 会开真 transport，单测不想跑真网络，所以我写了个 `_PreopenedSource` test helper 类，让 session 直接从 `create_connected_server_and_client_session` 传进来，绕过 `client.connect()`。

`tests/backend/smoke/test_tavily_mcp_live.py` — 2 个真机测，gated 在 `TAVILY_API_KEY` + `AGENTLOOM_RUN_LIVE_SMOKE=1`：

- `test_tavily_list_tools_lives_on_remote` — 打 `https://mcp.tavily.com/mcp/?tavilyApiKey=<key>`，断言至少有一个名字包含 `search` 的工具（Tavily 历史上把名字从 `tavily-search` 改过 `tavily_search` 改过 `search`，宽松匹配）
- `test_tavily_search_returns_non_empty_results` — 用 `MCPToolSource` 注册所有工具，找到 search 工具，真跑 `"agentloom github visual agent workflow DAG"` 查询，断言 `is_error=False` + content 非空

**两个真机测都过了**，`streamable_http_client` → `tools/list` → `tavily_search` 全链路打通。

---

## 当前总状态（截止本轮结束）

```
pytest tests/backend/
→ 119 passed, 4 skipped in ~10s
```

4 个 skipped 都是 gated 真机测：Volcengine (2) + Tavily (2)。手动跑 `AGENTLOOM_RUN_LIVE_SMOKE=1` 时 Tavily 确认过真机绿。

### 文件清单（backend/agentloom/）

```
api/           workflows.py chatflows.py
channels/      base.py (ChannelAdapter + ExternalTurn + FakeAdapter)
config/        (settings)
db/
  models/      tenancy.py provider.py chatflow.py workflow.py
               workflow_template.py channel_binding.py
               dashed_node_lock.py audit_log.py
  repositories/ base.py workflow.py chatflow.py
  base.py (engine/session maker)
engine/        workflow_engine.py chatflow_engine.py events.py
mcp/           types.py client.py tool_adapter.py bridge.py
providers/     base.py types.py openai_compat.py anthropic_native.py registry.py
rate_limit/    htb.py
schemas/       common.py chatflow.py workflow.py provider.py
system_workflows/ (占位)
tools/         base.py bash.py files.py search.py registry.py
main.py
```

### 测试清单（tests/backend/）

| 文件 | 数量 |
|---|---|
| `unit/test_providers_anthropic_native.py` | 16 |
| `unit/test_mcp_client.py` | 15 |
| `unit/test_tools.py` | 21 |
| `unit/test_schemas.py` | 22 |
| `unit/test_htb.py` | 9 |
| `unit/test_providers_openai_compat.py` | 9 |
| `unit/test_channel_adapter.py` | 3 |
| `unit/test_health.py` | 1 |
| `unit/test_repo_hygiene.py` | 1 |
| `integration/test_workflow_engine.py` | 4 |
| `integration/test_workflow_api.py` | 4 |
| `integration/test_chatflow_engine.py` | 4 |
| `integration/test_chatflow_api.py` | 3 |
| `integration/test_tool_loop.py` | 3 |
| `integration/test_repositories.py` | 4 |
| `smoke/test_volcengine_live.py` | 2 (gated) |
| `smoke/test_tavily_mcp_live.py` | 2 (gated) |

---

## 已完成的里程碑

| 里程碑 | 状态 |
|---|---|
| M0 仓库脚手架 | ✅ |
| M1 数据模型（Pydantic + SQLAlchemy） | ✅ |
| M2 OpenAI-compat + Volcengine 真机 | ✅ |
| M11 early: HTB 速率限制 | ✅ |
| M3 WorkFlow 引擎 + Postgres + FastAPI + SSE | ✅ |
| M4 ChatFlow + channel adapter hook + FakeAdapter | ✅ |
| M6 工具执行（Bash/Read/Write/Edit/Glob/Grep） | ✅ |
| M5 Anthropic 原生 + cache_control | ✅ |
| M7 MCP 集成 + Tavily 真机 | ✅ |

---

## 未完成 / 已知技术债

### 后端近期应做

1. **M8 / M9 前端 canvas** — React Flow 只读 → 交互（分支、合并、plan、execute）。目前后端 SSE 已经发事件，但没有任何消费端。**这是 MVP 用户可感知的最大缺口。**
2. **Provider / MCPServer CRUD REST + 持久化**
   - `ProviderRow` 表已有但没 CRUD API
   - `MCPServerConfig` **完全没有持久化表** — `0001_initial.py` migration 里没这张表，需要加 migration + ORM model + repository + API
3. **Engine 接入 MCP 工具源** — `MCPToolSource` 写好了但没有启动 hook，`ChatFlowEngine` / `WorkflowEngine` 构造时不会自动从 DB 加载 workspace 的 MCP servers

### 架构/合规性技术债

4. **`McpTool(server, tool_name)` 约束语法** — `schemas/common.py` docstring 列为示例，但 M7 只做了 `mcp__<server>__<tool>` 直通名。完整支持需要扩展 `Tool.constraint_keys()` 返回多个 `(name, detail)` 别名，再让 `ToolRegistry._matches` / `_visible` 遍历匹配。`tool_adapter.py` 顶部有 TODO 注释。
5. **MCP tool 跨 workspace 隔离** — `MCPToolSource` 不带 workspace_id。多 workspace 启用后需要每个 workspace 持有自己的 source 集合。
6. **用户认证 / OAuth** — 当前所有 API 硬编码 `DEFAULT_WORKSPACE_ID = "default"`，ADR-015 预留 M21 里程碑。

### 运行时 / 可观测性

7. **自动规划器（auto-planner）** — requirements 里提到的 agent 自动生成 plan 的能力，完全没做。
8. **Discord / Feishu 真实 channel adapter** — 目前只有 `FakeAdapter`。飞书对接是用户明确提过的（M5 选 Anthropic 的动机之一就是 CN 用户通过飞书访问）。
9. **Redis streams 替换内存 event bus** — `engine/events.py` 目前是进程内 `asyncio.Queue`，多实例部署会丢事件。
10. **React Flow 大画布虚拟化** — plan.md 风险表里记过，500+ 节点时需要 Zustand selectors 虚拟化。

### 烟测 / 对照测试

11. **Anthropic 真机烟测** — M5 只有单测，没有真机（没 `ANTHROPIC_API_KEY`，国内网络也是个问题）。应该补一个 gated live 测试类似 `test_volcengine_live.py`。
12. **ADR-013 跨 provider invariance 测试** — 目前每个 provider 的 wire 测各自断言顺序保持，但没有"所有 adapter 面对同一输入必须产生相同消息顺序"的对照测试。plan.md 风险表里列过这个。

---

## 第 10 轮 — M8 前端只读画布

启动前端，React 18 + TypeScript + Vite + Zustand v5 + React Flow v12 + react-i18next。

### 基础架构

- `frontend/` 骨架：Vite + TypeScript + Tailwind CSS + PostCSS
- `types/schema.ts` — 后端 Pydantic schema 的 TypeScript 镜像，手动同步
- `lib/api.ts` — 薄 fetch wrapper，Vite dev proxy `/api/*` → `localhost:8000`
- `lib/sse.ts` — `EventSource` 封装，按 backend 事件名订阅
- `store/chatflowStore.ts` — Zustand store，SSE 事件驱动状态更新
- `canvas/layout.ts` — 自研 DAG 布局（Kahn 拓扑排序 + 子树感知间距）
- `canvas/pathUtils.ts` — 路径解析，支持分支记忆
- `canvas/ChatFlowCanvas.tsx` — React Flow 主画布（水平布局，左→右）
- `canvas/ConversationView.tsx` — 右侧对话面板（聊天气泡 + 分支选择器）
- `canvas/nodes/ChatFlowNodeCard.tsx` — 自定义节点卡片
- `canvas/nodes/StatusBadge.tsx` — 状态标签
- `i18n/` — zh-CN + en-US 双语

### 核心功能

- 画布：拖拽、缩放、fitView、节点选中高亮
- 节点卡片：用户消息 + Agent 回复预览（截断 90 字符）、状态标签、虚线边框（planned/running）、合并节点紫色标记
- 对话面板：路径严格截止于选中节点、分支选择器、工作流下钻面板
- 边样式：实线（frozen→frozen）、虚线灰（planned）、紫色粗线（merge）、动画（running）
- 分支记忆：切换分支后再切回能恢复到之前的 endpoint
- 面板拖拽缩放（320px–900px）

### 测试

42 个前端测试通过（vitest + @testing-library/react）：
- `layout.test.ts` — 7 个（拓扑层级、稳定排序、像素坐标、悬空引用）
- `pathUtils.test.ts` — 8 个（路径解析、分支、严格截止）
- `chatflowStore.test.ts` — 12 个（加载、SSE 事件、分支记忆）
- `ConversationView.test.tsx` — 4 个（空态、路径渲染、分支切换）
- `ChatFlowCanvas.test.tsx` — 6 个（buildGraph、选中、虚线边）
- `App.test.tsx` — 2 个
- `i18n.test.ts` — 3 个

---

## 第 11 轮 — M9 前端交互（Round A）

在 M8 只读画布基础上，实现对话交互的核心链路。

### 后端新增

- `ChatFlowEngine` 增加 pending queue 管理：`enqueue`、`patch_queue_item`、`delete_queue_item`、`reorder_queue`
- `ChatFlowEngine` 增加失败节点管理：`delete_failed_node`、`retry_failed_node`
- `ChatFlowEngine` 增加取消正在运行的节点：`cancel_running_node`（通过 `asyncio.Task.cancel()`）
- `ChatFlowRuntime` 增加 `node_tasks: dict[str, asyncio.Task]` 跟踪节点执行任务
- 内部 WorkFlow 事件中继：`_relay_inner_events()` 订阅内层 workflow 事件，重新发布为 `chat.workflow.node.*`
- API 新增 7 个端点：
  - `POST /nodes/{nid}/queue` — 入队
  - `PATCH /nodes/{nid}/queue/{tid}` — 编辑队列项
  - `DELETE /nodes/{nid}/queue/{tid}` — 删除队列项
  - `POST /nodes/{nid}/queue/reorder` — 重排队列
  - `DELETE /nodes/{nid}` — 删除失败节点
  - `POST /nodes/{nid}/retry` — 重试失败节点
  - `POST /nodes/{nid}/cancel` — 取消正在运行的节点

### 前端新增

- 对话输入框：Enter 发送、Shift+Enter 换行
- **乐观更新**：发送消息后立即创建虚线 running 节点并 focus，不等 LLM 返回
- `_optimisticIds: Set<string>` 抑制 SSE 竞争：乐观节点存在时不处理 SSE 触发的刷新
- **Fork 语义**：从非叶子节点发送消息时，用 `submitTurn(parent_id=selectedNodeId)` 产生分支子节点
- 失败节点控制栏：重试 / 删除按钮
- 运行中节点控制栏：停止按钮（调用 cancel 端点）
- Pending queue 气泡：显示排队中的消息，可点 ✕ 删除
- **Markdown 渲染**：`react-markdown` + `@tailwindcss/typography`，对话面板和节点卡片均支持
- Greeting root 节点（`user_message = null`）正确渲染

### 节点位置持久化

- 后端 `NodeBase` 新增 `position_x: float | None`、`position_y: float | None`
- `_FROZEN_EXEMPT_FIELDS` 豁免位置字段，已完成节点也可移动
- 新增 `PATCH /api/chatflows/{id}/positions` 批量更新节点位置
- 前端拖拽结束后 debounce 500ms 自动保存到后端
- 加载时优先使用服务端位置，无保存位置时走自动布局

### 布局优化

- 子树感知布局：每个节点根据下游子树高度分配垂直空间，分支不再重叠
- 节点间距基于实际卡片尺寸（`NODE_WIDTH=192px + 100px` 列间距，`NODE_HEIGHT=160px + 50px` 行间距）

### 踩过的坑

1. **空白页崩溃**：旧 DB 数据没有 `pending_queue` 字段 → `undefined.length` 崩溃。修复：可选链 `?.`
2. **节点发送后等 LLM 完成才出现**：`submitTurn` 同步等待 LLM。修复：fire-and-forget + 乐观节点
3. **乐观节点一闪消失**：SSE `chat.node.created` 触发 `refreshChatFlow` 覆盖了乐观节点。修复：`_optimisticIds` 抑制 SSE 刷新
4. **Fork 不生效**：`enqueueTurn` 后端走 `_live_tip()` 始终追加到末尾。修复：改用 `submitTurn`（支持 `parent_id`）
5. **Turn 挂起不结束**：同上，enqueue 目标节点错误
6. **404 on enqueue**：后端未重启，新路由未加载

### 测试

- 前端 42 个测试通过
- 后端 125 个测试通过，4 个跳过（gated live 测试）

---

---

# Round 11 — M9 交互功能大批量推进 (2026-04-12)

## 思考/推理内容显示

- `ConversationView` 新增 `ThinkingBlock` 组件，收集 ChatNode 内所有 WorkNode 的 `extras.thinking`，折叠显示
- `WorkFlowNodeCard` LLM 调用卡片中新增 `ThinkingToggle`，可展开查看推理过程
- 火山引擎 `ark-code-latest` 需要显式传 `extra={"thinking": {"type": "enabled"}}` 才返回思考内容
- `openai_compat.py` 同时检查 `reasoning_content` 和 `thinking_content` 字段

## 节点视觉优化

- 根节点：蓝色左边框 `bg-blue-50 border-l-blue-400`，无左侧 handle
- 叶子节点：绿色边框 `bg-green-50 border-green-200`，无右侧 handle
- `ChatFlowCanvas` 和 `WorkFlowCanvas` 的 `buildGraph` 中计算 `isRoot` / `isLeaf` 并传入 node data

## 侧边栏 + 文件夹管理

- 新组件 `Sidebar.tsx`：树形视图，递归渲染文件夹和对话
- 文件夹 CRUD：创建（支持嵌套 `parent_id`）、重命名、删除（级联）
- 拖拽管理：HTML5 DnD，对话和文件夹均可拖入其他文件夹或拖至底部根目录区
- 折叠状态持久化到 `localStorage`
- 自定义弹窗 `InputDialog` 替代浏览器原生 `prompt()`
- 删除确认对话框使用 `fixed` 定位居中
- 后端：`FolderRow` 模型 + `FolderRepository` + REST API (`/api/folders`)
- 迁移：`0002_folders`、`0003_folder_nesting`

## ChatFlow 标题/描述/标签编辑

- 新组件 `ChatFlowHeader.tsx` 替代旧的静态应用标题栏
- 标题、描述：点击即编辑，Enter 提交，Escape 取消
- 标签：圆角小标签，hover 显示删除，点 "+" 添加，支持 Enter 和逗号确认
- 后端：`ChatFlowRow` 新增 `description` (Text) 和 `tags` (JSONB) 列
- `PATCH /api/chatflows/{id}` 端点，使用 `model_fields_set` 判断传入字段
- 迁移：`0004_chatflow_description_tags`

## Provider 设置页

- 后端 `ProviderRepository`：CRUD + `resolve_api_key()` + `list_all()`
- REST API `/api/providers`：GET/POST/PATCH/DELETE + `/test` 测试连接 + `/models` 发现模型
- 前端 `ProviderSettings.tsx`：弹窗式设置面板
  - 服务商列表 → 创建/编辑表单
  - 支持 OpenAI 兼容和 Anthropic 原生两种类型
  - API Key 来源：环境变量 / 内联输入
  - 测试连接按钮
  - 模型发现 + 标记默认模型（pinned）
- 设置按钮（齿轮图标）集成到 `ChatFlowHeader` 右侧
- 引擎动态选择：`_provider_call_from_settings()` 优先从 DB 读取 provider，回退到 `VOLCENGINE_API_KEY` 环境变量

## 右键上下文菜单

- `ChatFlowCanvas` 新增 `onNodeContextMenu` 处理器
- `NodeContextMenu` 组件：根据节点状态动态显示菜单项
  - 始终显示：进入工作流
  - `failed` 节点：重试
  - `running` 节点：取消执行
  - 可删除节点：删除（叶子）/ 删除及所有后续节点（非叶子）
- 点击画布空白处或再次右键关闭菜单

## i18n

- 新增键：`sidebar.*`、`app.untitled/add_description/add_tag/no_chatflow`、`chatflow.ctx_*`、`providers.*`
- en-US 和 zh-CN 完整对称

### 测试

- 前端 42 个测试全部通过
- i18n key 奇偶性测试通过

---

## 建议下一步

M9 收尾：
- WorkFlow 编排编辑（拖拽调整节点、编辑 plan）
- Playwright e2e 基础测试

M10 System Workflows：
- merge、plan_elaborate、title_gen、compact

M13 MVP 验收
