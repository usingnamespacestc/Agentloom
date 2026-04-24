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

# Round 12 — 节点卡片 / 设置 / 模型默认值打磨 (2026-04-12)

## 画布节点卡片

- 节点卡片宽度 w-48 / w-52，完整显示节点 ID
- 新增 `NodeIdLine` 组件（`canvas/nodes/NodeIdLine.tsx`）：点击复制 ID，带 "copied!" 900ms 反馈；受 `preferencesStore.showNodeId` 开关控制
- 两种卡片（ChatFlow / WorkFlow）都在 token 进度条下方渲染节点 ID
- `layout.ts` 的 `NODE_WIDTH` 192→208 适配新宽度

## 对话面板元信息

- `ConversationView` 新增 `MetaFooter`：
  - 节点 ID（从消息前移到消息后）
  - 每轮 token 用量（`↑prompt ↓completion (N cached)` — 加了上下箭头才让用户不再误以为是累计值）
  - 生成时长（`aggregateWorkflowUsage` / `durationSeconds` / `formatDuration` 辅助函数）
  - 生成速度（completion_tokens / 秒）
- 三个新开关：`showTokens`、`showGenTime`、`showGenSpeed`（`preferencesStore` + `Settings` 中 Canvas 页）

## 全局 vs 对话设置

- `Settings.tsx`：标题 "Settings" → "全局设置"；Canvas 页改为 `rows` 数组渲染 4 个 checkbox
- 新增 `ChatFlowSettings.tsx`：ChatFlow 级设置弹窗。入口按钮 "⚙ 对话设置" 位于右上，与 "全局设置" 并列

## Provider 编辑器打磨

- 修掉"双加"Bug：Test 会先 `createProvider`，再点 Save 又创建一次。改为维护 `persistedId` + `createdInSession` 两个本地状态；一旦 Test 已落库，Save 走 `patchProvider`；Cancel 若本轮创建过则清理孤儿行
- 移除 Ollama / LMStudio preset：它们是 OpenAI 兼容的特例，不该单独列。改为在 preset 下拉下方展示一段 `local_provider_hint`，告诉用户 "选 Custom + OpenAI 兼容 + /v1 地址 + 无需密钥"
- "默认"→"收藏"：`providers.pinned` 标签改名，因为模型可以多选，"默认"应该只有一个
- 新增模型上下文窗口字段（`context_window` 早就在 schema 里，只是没 UI）：
  - 每个模型行加一个紧凑输入框
  - 支持 `4096` / `32k` / `128K` / `1m` / `1.5M` 等写法，k=1024、M=1024²，大小写均可
  - 显示遵循统一规则：>=1M → M，<1M → k（`lib/tokenFormat.ts` 的 `formatTokensKM` 同时给 Settings 和 Canvas TokenBar 用；对话面板保持原始 `↑prompt ↓completion` 不变）

## 默认模型：从 chat/work 分裂到单一字段

走过三步：

1. 先做了 `default_chat_model` + `default_work_model` 两个下拉 + 删掉 "使用系统默认" 选项
2. 后端实现 `_resolve_default_model`：给一个可能过期的 `ProviderModelRef`，校验是否还在线；过期或为空则回落到 "第一个 pinned → 第一个 available → None"
3. 用户追问："咱们对话本身也是在工作流里跑，chat/work 分裂逻辑不清楚"——确实，当前代码里 `default_work_model` 根本没地方读；follow-up LLM 走 `parent_llm.model_override` 继承。于是**塌成单一 `default_model`**

最终一个字段路径：

- `ChatFlow.default_model: ProviderModelRef | None` —— schema / DTO / API 一致
- 创建 ChatFlow 时 `create_chatflow` 自动填入（第一个 pinned → 首个可用）
- `get_chatflow` 里 lazy-rehydrate：拿到的 `default_model` 若 provider/model 已被删，自动重解并 persist
- 引擎：`_spawn_turn_node` 把 `chatflow.default_model` 写进 seeded LLM_CALL 的 `model_override`；`_run_llm_call` 把它格式化成 `provider_id:model_id` 交给现有的 `_provider_call_from_settings`
- Pydantic v2 默认 `extra="ignore"`，老行里残留的 `default_chat_model` / `default_work_model` 字段在 `model_validate` 时直接丢掉，lazy-rehydrate 会补上新的 `default_model`。老 ChatFlow 迁移零改动

## Patch 与 runtime 同步 Bug

用户发现改了 `default_model` 后端有落库，但前端重开时看到的仍是旧值。原因是 `get_chatflow` 优先返回 `runtime.chatflow`（为了让后台任务的结果不被 DB 陈旧态覆盖）。PATCH 端点当时只改 DB 没改 runtime。修复：`patch_chatflow` 里若 engine 有 runtime，把 title/description/tags/default_model 都同步到 `runtime.chatflow` 的内存副本，保证下一条 GET 和下一次 turn submission 都见到新值。

## 根节点不可删除

双层防护：

- 前端 `computeUndeletableIds` 把所有 `parent_ids` 为空的节点加入 undeletable 集（此前只挡了 running 节点及其祖先）—— 卡片上的 × 按钮和右键菜单的删除项都会消失
- 后端 `ChatFlowEngine.delete_node_cascade`：若目标在 `chat.root_ids` 里，抛 `ValueError("cannot delete root node ...")`；API 层翻译为 HTTP 409

## 标题展示统一

用户指出左边侧边栏未命名对话显示的是"4月12日 14:30"（创建时间），顶栏却显示 "未命名对话"，一会儿时间一会儿文案，不一致。

- `ChatFlow` schema 加 `created_at: datetime = Field(default_factory=utcnow)` 字段；前端 `types/schema.ts` 同步
- 新建 `lib/chatflowLabel.ts` 的 `chatflowDisplayTitle(cf)`：title trim 后非空就用 title；否则用 `created_at` 格式化；再不行退回 `id.slice(0, 8)`
- `Sidebar` 删除本地 `untitledLabel`，改用共享 helper
- `ChatFlowHeader` 的 `EditableTitle` 加 `emptyLabel` prop，展示态用共享 label；输入态的 `placeholder` 保留 "未命名对话" 作为打字提示

## 其他细节

- Pydantic `ChatFlow` 新字段不破坏旧数据：`extra="ignore"` + `default_factory`
- 前端 `chatflowStore.patchChatFlow` 的类型签名 / 乐观更新 / test 夹具全跟进
- i18n 清理：删掉 `use_system_default` / `default_chat_model*` / `default_work_model*` / `preset_ollama` / `preset_lmstudio`；新增 `default_model`（+hint）、`context_window`（+hint）、`local_provider_hint`、`canvas_prefs.*`

## `.gitignore` 踩坑

`.gitignore` 里的 Python 规则 `lib/` 把 `frontend/src/lib/` 下任何新文件都一起忽略了（`api.ts`、`sse.ts` 是在规则生效前就 track 的，所以没受影响）。`tokenFormat.ts` 和 `chatflowLabel.ts` 都要 `git add -f` 强加。后续应把该规则改成 `/lib/` 或者更精确的 Python 路径，不然每次在 `src/lib/` 下加文件都会再踩一遍。

---

## 建议下一步

M9 收尾：
- WorkFlow 编排编辑（拖拽调整节点、编辑 plan）
- Playwright e2e 基础测试

M10 System Workflows：
- merge、plan_elaborate、title_gen、compact

M13 MVP 验收

---

# Round 13 — 2026-04-12 · M10 设计收口

这一轮没写功能代码，通过一连串对话把 M10（系统工作流 + Plan/Judge 管线 + 关键帧）锁定成了可以落地的设计。先背景后决策。

## 触发这一轮的三条观察

1. 再读需求 §3.5 时发现 `plan_elaborate / merge / compact / title_gen` 只有名字没结构，真动手会发现"用 Python 写死还是用 Template 写"这个问题从未定过
2. 观测目标 ChatNode `019d8436-5c09-78c0-97dc-01aa5020d38f` 需要一个 CLI，顺手把 `inspect_workflow.py` 写出来。跑起来后发现当前 WorkFlow 已经是 ReAct loop（之前我一度错判成单步调用），`MAX_TOOL_LOOP_ITERATIONS=12` 在 `_spawn_tool_loop_children` 里，这件事澄清完以后对 M10 的形状立刻清晰
3. Tavily 一直未被触发的谜团 —— `MCPToolSource` 有 lib 代码但没被 main/engine/api 引过，M7 只验了 unit test。MCP runtime wiring 正式归为 M7.5 挂在 backlog

## 核心拍板链（按对话顺序）

### 系统工作流走"纯 Template"路线，不做混合

我一开始推"Python 结构 + DB params"的混合方案，用户一句话否掉："那就失去了 WorkFlow 本身的可见性"。最终定的 ADR-019：template 引擎支持 Jinja 风格 `{{ param }}` 和 `{% include 'other_builtin_id' %}`，加载时检测引用循环；系统工作流作为 YAML fixture 迁入 `workflow_templates` 表，用户同名 `builtin_id` 覆写 = 编译器被改写

### 执行模式 × Judge 开关矩阵

用户说"要参考 claude code 的 planner / executor / judge 分工，但主打可控 agent 要支持半自动"。拆了几轮之后得到三模式三开关：

- `direct / semi_auto / auto` 三模式作为一个顶层选择器，其下是 `plan / judge_pre / judge_during / judge_post` 四个开关
- `direct` 就是现在的 ReAct，什么都不做
- `semi_auto` 开 plan + judge_pre，judge_during / judge_post 可选；这是关键帧能用的唯一模式
- `auto` 四开关全开，端到端跑，只在 `judge_pre.feasibility != "ok"` / `judge_during.verdict == "halt"` / 任何 WorkNode failed / revise 预算耗尽时停下

`auto_mode_revise_budget: int = 3` 挂在 ChatFlow 上，WorkNode 可 override。停下时推浏览器本地 toast + 标题闪烁，外部通道留给后面

### `task_frame` 被吃掉，judge 拆成三个

中间曾经提过 `task_frame` 作为"planner 执行前生成三元组"的前置环节。用户一眼看穿："这就是前置 judge，为什么不直接归到 judge 家族？"然后进一步提议把 judge 拆成三个开关：

- `judge_pre`：填三元组 + 可行性评估 + blockers + missing_inputs；半自动下用户可编辑产出并"再判一遍"；自评"不行"也允许用户硬来
- `judge_during`：用户明确说要"对执行方案和代码细节进行批判，尽可能反驳挑问题"。参考 claude code 的 `verificationAgent.ts`（`src/tools/AgentTool/built-in/verificationAgent.ts`），那个 prompt 最精彩的是**反自辩清单**（"code looks correct" / "tests pass" / "probably fine" 被列出来并禁止）。judge_during 走同样的红队定位。触发时机定 A+C：plan 产出新 WorkNode 草稿后（A），以及 llm_call 输出包含写类 tool_use 的最后一公里（C）。MVP 只做监督式，verdict = halt 不真的中断，只记录给用户
- `judge_post`：产出增加 `issues: [{location, expected, actual, reproduction}]`，`location` 是 WorkNodeId，让 UI 能跳转高亮。这是我们相对 claude code 天然占优的地方 —— 有 DAG，能定位到节点

三个 judge 都作为 `step_kind="judge_call"` 的 WorkNode 进 DAG，过程完全可见，可以点开看 prompt 和裁决，右键"再判一遍"会新建 sibling `judge_call`，历史不覆盖。这条是 ADR-018

### 关键帧（keyframes）

这个需求在前几轮被我漏掉了，用户回头补上。定义是：半自动模式下用户手动在画布上预放的 dashed WorkNode，作为 planner 必须经过的锚点。关键几条：

- 用户只填三元组，`step_kind` 由 planner 决定（选 B 不选 A，否则和锁的语义打架）
- 每个关键帧带锁：🔒 = planner 完全不能动；🔓 = planner 可以改字段、位置、step_kind，但要保留 `keyframe_origin_trio` 做对比/回滚
- 用户画的边是硬约束，和锁无关。planner 不能绕过、不能反转、不能删除
- `auto` / `direct` 模式下禁用关键帧 —— 摆一个就定义上变成半自动了

### 递归

WorkNode 允许递归包含 WorkFlow，三条硬约束：context 跨层不透明（保住 ADR-009）、深度上限 `MAX_WORKFLOW_DEPTH=5`、进入交互统一用"打开工作流"而不是特殊化双击。每一层都有自己独立的一套三元组 + 开关 + 关键帧 + 执行模式 + `next_model_override` 链

### `next_model_override` 链与"变化点"可视化

模型选择的可见性问题 —— 全部边都标模型名太吵，只标节点又丢了"这是给谁用的"信息。定下来：

- 每个节点有 `next_model_override: ProviderModelRef | None`；descendant 走祖先链取第一个非空值，再 fallback 到 `default_work_model`
- UI 只在变化点（`next_model_override != null` 的节点）的出边上标模型名，继承边完全空白（连 ↓ 都不用，用户说"如果继承链显示得好的话"）
- ChatFlow 根永远是变化点（它是种子），所以图上至少总有一条有标签的边
- 悬停任意边高亮整条继承链

这是 ADR-022

### 单根 ChatFlow

之前 §3.2 说"允许多根"，实践上其实一直单根。这次顺手把 `root_ids: list` 收窄成 `root_id: NodeId`。好处是根节点可以当 next_model_override 的唯一种子，Sidebar 保持平坦列表

### Tool loop budget 晋升成配置项

`MAX_TOOL_LOOP_ITERATIONS` 从常量升为 `ChatFlow.tool_loop_budget: int | null = 12`（null = unlimited），WorkNode 可 override。对极端长任务开放

### Kill 对话设置里的模型选择

根节点单根化之后，对话设置里的"默认模型"下拉其实没必要单独存在 —— 直接在 Root 上选就好。保留 `default_work_model` 字段作为"哪里都没设"的兜底，但 UI 上那个下拉可以砍。Round 14 实装的时候顺手处理

## 副产物

- **`backend/scripts/inspect_workflow.py`**：给 UUID 自动识别是 ChatFlow / ChatNode / WorkFlow / WorkNode，打印包裹链 + 拓扑序 WorkNode 清单（step_kind / status / parents / model / usage / tool_name+args / tool_result）。对着运行中的 DAG 排查 plan/judge 的时候非常舒服
- **全局设置 Canvas 面板加 `showChatflowId` 开关**：用户顺手要求的。勾上后 `ChatFlowHeader` 里标题后面出现可点击 copy 的灰色小 id，和每个节点右上角的 node_id 开关对称
- **MCP runtime wiring** 正式挂到 M7.5 backlog，和 M10 解耦。这一次先不动

## 写入的文档

1. `requirements.md`：
   - §3.1 Node 加 `inputs`、`next_model_override`
   - §3.2 ChatFlow 收窄到单根
   - §3.3 WorkFlow 加三元组、`judge_call` step_kind、递归三约束
   - §3.4 全改：§3.4.1 执行模式矩阵、§3.4.2 关键帧规则
   - §3.5 重写：纯 Template + 四类 builtin（`plan / judge_pre / judge_during / judge_post` + 原有四个）
   - §4.8 Node locking 区分常规锁 vs 关键帧锁
   - §4.9 Keyframes 作为硬约束
   - §4.10 `next_model_override` 继承链
   - §5.3 / §5.4 / §5.6 新增 FR 条目（模式选择器、关键帧、budget、re-run judge、template 引擎要求等）
   - ADR-008 重述；新增 ADR-018 ~ ADR-022
   - Appendix A schema 重构 + 新增 `JudgeVerdict` / `Critique` / `Issue` / `WorkflowTemplate.builtin_id`

2. `plan.md`：M10 整体重写，拆成 M10.0 ~ M10.6 六个子阶段，给了每个子阶段的 deliverables + acceptance + 具体 test 文件清单。大意：schema 迁移 → template 引擎 → engine wiring（读 execution_mode 分支）→ judge 原语 → 自动模式 halt + re-run API → 前端模式选择器和关键帧 UI → 清除 Python 硬编码残留。Playwright e2e 也挂到这里

## 故意没动的

- MCP runtime wiring（M7.5，单独做）
- 通知外部通道（webhook / desktop / 邮件）（M10 之后按需加）
- judge_during 的中断模式（MVP 是 monitoring 模式，中断模式挂在 post-MVP 选项）
- 字符级编辑溯源 / CRDT（仍然 v2+）

---

# Round 14 — 2026-04-13/14 · 递归 planner 全量落地 + 自动模式 e2e 跑通

Round 13 收口的 M10 设计这一轮全部代码落地，并且在跑复杂多步任务时把原本"plan → judge → executor"线性结构升级成 **planner 自身可决定 atomic / decompose 的递归形态（M12）**。最终自动模式 e2e 跑通：mixed / parallel / sequential / nested 4 种 decompose 形态 + clarify-then-succeed 多轮 halt-then-resume 流。

## 主线：M10 → M11 → M12

### M10：按 Round 13 的子阶段切

`M10.0` schema 迁移（trio + execution_mode + judge 字段）→ `M10.1` template 引擎 + 4 个 builtin fixture（plan/judge_pre/judge_during/judge_post）→ `M10.2a` effective_model_for walker + per-WorkFlow tool_loop_budget → `M10.3` judge_call step handler → `M10.4-6` execution-mode orchestration + judge re-run API。

### M11：model picker 改版

composer-model picker 取代节点上的小 badge；边 hover ribbon 替代 ring 高亮；ChatFlowNode 落地 `resolved_model` 快照（spawn 时冻结）。

### M12：递归 planner

跑 5 份并行长文 / 3 阶段串行分析这种任务，原 M10 的线性结构会把所有 step 摊在一层 WorkFlow 里，judge_pre 一眼看不完。重做成 planner 自己决定要 atomic 还是 decompose；每个子任务是 `SUB_AGENT_DELEGATION` 节点，内部带独立 sub-WorkFlow，递归走完整 plan/judge/work/judge 管线。

子阶段：`M12.1` schema（sub_workflow / parent_ids / SUB_AGENT_DELEGATION）→ `M12.2` nested drill-in + breadcrumb → `M12.3` planner/planner_judge/worker/worker_judge 4 模板 → `M12.4a/b/c` spawn trio fixture / typed parser / debate-as-chain（多轮反馈循环）→ `M12.4d1~d6` decompose 拉满（schema → engine 递归 → orchestrate spawn → aggregating judge_post → retry+redo_targets → 跨轮 concerns thread）→ `M12.5` role-based 渲染。

## 期间踩的并发 / 依赖 / halt 坑（按调试顺序）

### 坑 1：sub-layer halt 自动 bubble 到上层 ChatFlow

子层 judge_post halt 通过 pending_user_prompt 一路冒泡，sibling 还在跑就被整个 chatflow 拉停。**Phase 1**：把 halt 吸收到 `SUB_AGENT_DELEGATION` 节点身上当 fail，由父层 aggregator 决定要不要 halt。语义从"任意 sub 失败=全停"变成"父层重判"。

### 坑 2：aggregating judge_post 看不到失败细节

sub fail 时只有 status，aggregator 不知道为什么失败。**Phase 2**：扩 `upstream_summary` 带 failure 类型 + 子层最后一条 critique；judge_post 模板新增 failure 词汇表，让 aggregator 能决定 redo / accept_with_caveats / halt。

### 坑 3：retry 和 revise 共用预算

只有一个 `revise_budget`，复杂任务的 judge retry 一不小心吃光。**Phase 3**：拆独立 `judge_retry_budget`（默认 3，-1=无限）+ ChatFlow 设置 UI。

### 坑 4：跨轮 redo 丢上轮 issues

同轮内 redo_targets OK，但下一轮 worker 拿不到上轮被指出的具体 issues。补跨轮 carry-forward + 全 round concerns 拼进 judge_post 输入。`b437d08` 顺带修了 `_atomic_brief_for_worker` 在 redo-cloned worker 上的索引错。

### 坑 5（这一轮重点）：decompose 子任务拿不到上游产出

`mixed_chinese` e2e 暴露：综述子任务应基于前置 `bio_a/b/c` 输出做对比，但 spawn 时 sub-WorkFlow 的 `inputs` 和 judge_pre `input_messages` 已经冻结，不知道上游会产出什么。

**Option A**：在 `_after_delegation` 加 `_inject_upstream_outputs_into_ready_children` hook，遍历刚 finish 的节点的 child `SUB_AGENT_DELEGATION`，当所有 `SUB_AGENT_DELEGATION` parents 都 SUCCEEDED **且** sub-WorkFlow 还全部 PLANNED 时，把上游 effective_output 拼进 sub.inputs 并 re-template judge_pre 的 input_messages。`NodeStatus.PLANNED` 守门保幂等。修完后 mixed e2e 的综述节点能正确引用三个 bio 的具体人名 + 共性/差异。

## 测试

`tests/e2e/_helpers.py` 加 `run_headed_multi_turn`：复用 `run_headed_turn` 的 setup（new chat / 切 mode / patch 元数据 / 选模型），然后循环 prompts → fill → send → poll → report。任一轮非 succeeded 就停，但 chatflow 不删，方便 UI 排查。

新建：

- **`multi_turn_chinese_headed.py`** — 3 轮：5 份独立科普长文（parallel）→ 3 阶段串行分析（sequential，stage B/C 必须引用前阶段）→ 4 章入门小册子（nested decompose）。第二次跑：turn 1 decompose 9 顶层/39 总；turn 3 decompose 4 章；turn 2 atomic（planner 选择，不算 bug）。
- **`clarify_then_succeed_headed.py`** — turn 1 模糊 prompt → judge_pre `infeasible` → judge_post `fail` → agent_response 是补全清单（19s）；turn 2 用户补全具体活动信息 → judge_pre `ok` → 完整方案 4 部分齐全（98s）。`pending_user_prompt` + 跨轮 chat-context 整条流跑通。

实测：mixed 212s ✅、clarify turn1 19s + turn2 98s ✅、multi-turn 3 轮全过。

## 副产物 / 顺手修

### 前端 context-% bug

ChatNode token bar 写死 32k 分母，44k 就显示 100%。改：`ChatFlowCanvas` fetch providers，建 `contextWindowMap`，按 `resolved_model` → `default_model` 链解析每节点真实 `context_window` 传给 `TokenBar`；fallback 仍是 32k。`seed_volcengine_provider.py` 同步加 `context_window=128_000`（注意：seed 是一次性，存量 provider 行需 Settings UI 手动补）。

### 隐藏 semi_auto

M10 定的三模式中 `semi_auto` 实装太重（要画布关键帧 UI），先在 mode slider 隐藏，只暴露 direct / auto。代码留着，等关键帧 UI 一起做。

### 其他小修

- `8ed495f`：judge_pre `risky` verdict 改"带 assumptions 当 handoff notes 继续"，只有 `infeasible` 才真 halt
- `efc3e9d` / `ac68379`：judge 输出 JSON 解析失败时给一次自我修正机会；list 字段 null 容错

---

## 2026-04-16 — 画布 Sticky Notes + Planner 容错 + 多项 UI 改进

本轮大量修补前端体验和后端容错逻辑。

### Planner corrective retry

模型（尤其 Ollama 小模型）有时返回的 JSON plan 缺少引号导致解析失败。改 `_after_planner_judge`：`PlannerParseError` 时如果 planner 数 < 2，重新 spawn planner 并把上一次坏输出 + 解析错误作为 correction context 带入，给模型一次自我修正机会。超过 2 次仍失败则正常 halt。

### 画布 Sticky Notes（完整端到端）

支持在 ChatFlow 画布和 WorkFlow 画布上右键 → 「插入文本框」：

- **StickyNoteNode**：黄色便签，可编辑标题（原为固定 "Note"）和正文，可拖拽和调整大小（NodeResizer），支持右键 → 「删除文本框」
- **后端持久化**：`StickyNote` 模型存入 ChatFlow / WorkFlow 的 JSONB payload，`PUT /sticky-notes` 端点接受 `sub_path` 参数支持嵌套子工作流
- **Frozen-node 豁免**：`_strip_frozen_exempt` 递归剥离 `sticky_notes`，保证在已完成 ChatNode 的内部工作流上编辑便签不触发 frozen guard
- **前端**：debounced 800ms 自动保存位置/尺寸/内容变更；刷新后恢复
- **交互修复**：原本用 `startsWith("_sticky_")` 识别便签节点，但后端持久化后重新加载的便签 id 是 UUIDv7 不带前缀，导致选中/拖拽/右键/双击对已保存便签全失效。改用 `stickyNotesRef` 运行时查表识别；同时 `zoomOnDoubleClick={false}` 避免双击进入编辑态时误触 d3-zoom 页面放大

### 其他改动（本轮，来自上一压缩周期尾部）

- **Retry 使用新模型**：5 文件全栈改动 — `RetryRequest` 接受 `ProviderModelRef`，engine 优先 caller-supplied 模型，前端传 `composerModels`
- **pre_judge `risky` 不再 halt**：`_judge_pre_should_halt` 只看 `infeasible`，`risky` + `missing_inputs` 继续执行
- **judge merged_response / redo_targets 显示**：右侧面板 JudgeBubbleBody 渲染这两个字段
- **fallback max_tokens = 8192**：模型无 `max_output_tokens` / `context_window` 配置时保底
- **可拖动 composer 区域**：拖拽手柄调整输入区高度，textarea 随区域伸缩
- **模型选择器不被遮挡**：composer 容器 `overflow-visible`
- **一键复制消息**：用户 / 助手消息各有复制原始文本按钮
- **Judge tool_use structured output**：judge 调用通过 `judge_verdict_tool_def` 生成工具定义，不强制 `tool_choice`（Ollama 忽略该参数）

## 2026-04-16 (续) — Structured JSON output 支持（provider / model 双层 json_mode）

Planner corrective retry 只是 band-aid：原 bug 是模型吐出缺引号的 JSON 被 `json.loads` 拒绝。真正的根治是用 provider 侧的 structured output 能力（Ollama format+schema、OpenAI response_format、DeepSeek json_object）把 JSON 合法性从"提示工程祈祷"升级成"协议级约束"。

不同 provider 的 JSON 原生能力分三档，所以增加一个 `JsonMode` enum 让用户在 Settings 里挂配置：

| 档位 | 语义 | 典型 provider |
|---|---|---|
| `schema` | `response_format={"type":"json_schema","json_schema":{...}}` | Ollama, Volcengine Ark（新版 Doubao）, OpenAI gpt-4o+ |
| `object` | `response_format={"type":"json_object"}`（只保合法 JSON，不校验 schema） | DeepSeek, Moonshot, GLM |
| `none` | 不发 `response_format`，继续靠 prompt 约束 | Anthropic（走 tool_use）、未知 provider 的保守默认 |

实现分两层：

- **Provider 级默认值**：`ProviderConfig.json_mode: JsonMode = NONE`。新建时默认不开，用户在 Settings 里按 provider 文档手动选档。
- **Model 级可选覆盖**：`ModelInfo.json_mode: JsonMode | None = None`。留空 = 跟随 provider；覆盖时 per-model 优先（混合能力 fleet 的逃生舱，比如一个 provider 下一批老模型只支持 `object`、新模型支持 `schema`）。Resolve 逻辑 `model.json_mode or provider.json_mode`，发生在 `_provider_call_from_settings`。

### Backend wiring

- `schemas/provider.py` — 新增 `JsonMode` enum + 两个字段；JSONB payload 无需迁移，历史行 deserialize 时缺字段就取默认 `NONE` / `None`。
- `providers/base.py` / `openai_compat.py` / `anthropic_native.py` — `chat()` 签名加 `json_mode` + `json_schema`。OpenAI-compat 根据 `json_mode` 转成 `response_format`；**tools 同时出现时跳过** `response_format`（大部分 provider 互斥，tool JSON 本身就是 structured output）。Anthropic 原生是 tool_use 走 structured output，所以这两个参数 no-op。
- `engine/workflow_engine.py` — `_invoke_and_freeze` 加 `json_schema` 参数；`_run_llm_call` 针对 `WorkNodeRole.PLANNER` 把 `RecursivePlannerOutput.model_json_schema()` 塞进去。`object` 档的 provider 自动退化为自由 JSON（不校验字段），已经比 `none` 强得多。
- `api/workflows.py::_provider_call_from_settings` — 闭包签名加 `json_schema`，在 DB config 里 resolve `json_mode` 后转给 adapter。
- `api/providers.py` — CRUD `CreateProviderRequest` / `PatchProviderRequest` 加 `json_mode` 字段。

### Frontend

- `lib/api.ts` — 新增 `JsonMode` 类型，`ProviderSummary` / `ProviderDetail` / `CreateProviderBody` 加 `json_mode?`，`ModelInfoDTO` 加可空 `json_mode`。
- `components/Settings.tsx` — provider 表单加「JSON 输出模式」下拉（none / object / schema + 说明），每个 model 行多一个小下拉 `inherit / none / object / schema`（空值映射到 `null` = 跟随）。新增 7 条 i18n（中英文各一份）。

### 测试

- `test_providers_openai_compat.py` 新增 4 个单测：
  - `test_json_mode_schema_sets_response_format_json_schema`
  - `test_json_mode_object_sets_json_object_response_format`
  - `test_json_mode_none_omits_response_format`
  - `test_json_mode_skipped_when_tools_present`
- 既有 210 unit + 96 integration 全绿。现有 provider_call stub 都用 `**_kwargs` 接，所以新增 `json_schema` 参数对测试 double 透明。

### 刻意没做的

- Anthropic native 也可以用 tool_use forcing 跑 structured output，本轮只把签名对齐、未实现转义逻辑。planner 目前只在 openai_compat 路径下工作，后续如有需要在 Anthropic 原生 provider 跑 planner 再加。
- `response_format` + `tools` 共存的 edge case（OpenAI o1 系列 / Anthropic 结构化）未覆盖；当前策略是"有 tools 就跳 response_format"，够用。
- Provider 发现阶段（discover_models）不自动填 `json_mode` — provider 不一定在元数据里标注能力，留给用户按文档手动选。

## 2026-04-17 — Planner reasoning / DB 连接池漏 / 引擎 session 生命周期 / provider_sub_kind

本轮集中拆历史 incident 遗留的几颗定时炸弹，并把按模型调参数这条线补齐。

### #115 — Planner 降级用 response_format=json_schema

修 planner 偶尔回 markdown-fenced JSON 的问题。根因：`_run_llm_call` 调 planner 时 `expose_tools=True`，让 `_invoke_and_freeze → _to_wire_tools` 返回非空 `wire_tools`；`openai_compat.chat` 的 "tools 存在时跳过 `response_format`" 互斥逻辑就把 planner 的 `json_schema` 默默吞了，退化回纯提示词约束。

改动：`workflow_engine._run_llm_call` 对 `node.role == PLANNER` 强制 `expose_tools=False`，并把 `RecursivePlannerOutput.model_json_schema()` 传下去。现在 planner 路径固定走 `response_format={type:"json_schema",...}`，模型做不出合法 JSON 就直接 400 到重试循环，不再默默吐坏 JSON 让下游解析器救场。

### #116 — Planner 输出加 `reasoning` 字段

`RecursivePlannerOutput` 顶层多一个 `reasoning: str | None`，让 planner 在承诺 atomic/decompose 之前先用 1–2 句交代判断依据。prompt 里附了说明：模型若已有 thinking channel（Anthropic 扩展思考、DeepSeek reasoning_content），这里写结论摘要即可，别重复 full trace。

不对模型的 thinking 能力做分叉——统一字段对非思考模型也有好处（外显的元认知 trace 对 judge 的诊断有用），而且字段 optional，思考模型留 null 也不罚。

### #117 / #118 / #119 — DB 连接池耗尽 root cause

现象：跑一个 53 分钟的大工作流时，`/api/providers` 突然爆几个 500；其他请求也偶发 QueuePool timeout。

诊断过程：

1. 最开始怀疑是并发调 provider API 飙高，但日志显示问题期 `/api/providers` 并没有流量激增。
2. 查代码发现 `api/chatflows.py::submit_turn` 是 `async def` + `Depends(get_session)`，整个 handler 生命周期持一个 session → 一个 DB 连接。
3. `submit_turn` 内部调 `engine.submit_user_turn(...)` 会 `await future`，等整个 workflow 跑完才 return。workflow 可能几十分钟。期间这条连接一直被这个 handler 持着，池里剩 14 个给其他请求（`pool_size=5, max_overflow=10`，下面立刻扩到 20+30=50）。
4. 与此同时 workflow 跑出几十个 planner / judge，每个都 `async with get_session_maker()()` 开短 session 查 provider，也要抢池里的连接。连接数压力和工作流时长成正比。

修法分两步：

- **#118 短期缓解**：`db/base.py::configure_engine` 把 `pool_size` 从 5 提到 20，`max_overflow` 从 10 提到 30（50 并发连接上限，给 50 并发 sub_workflow 留够）。
- **#119 根治**：`api/chatflows.py::submit_turn` 拆成 Phase 1 / 2 / 3 —— Phase 1 用一条短 session 从 DB 加载 ChatFlow 挂上 engine；Phase 2 把 session close 掉再 `await engine.submit_user_turn(...)`（几十分钟的长 await 完全不占连接）；Phase 3 用新的短 session 保存最终状态。
- FastAPI 侧加 `get_session_scope()` 依赖（返回 `async_sessionmaker` 而不是 session），handler 自己管 session 生命周期，测试同样通过 `app.dependency_overrides[get_session_scope]` 换成测试 DB 的 sessionmaker。

最初只把 `get_session_maker()` 直接调掉，结果集成测试开始 404：因为绕过了 FastAPI 的 `dependency_overrides`，测试客户端走到了生产 DB 连接配置。改成通过 `Depends(get_session_scope)` 注入后，测试 fixture 正常 override，306/306 全绿。

### #120 — 七层工作流诊断

用户反馈一个任务跑到了第 7 层 planner 递归。把 ChatNode `019d98a5-c0ff-7d43-909a-b7fa44ff869c` 的完整 workflow tree 拉出来数：

- 实际最大嵌套 12 层、共 117 个 planner、267 次 LLM 调用
- 42 次 `ReadTimeout` 来自 Ollama（上面的 #118/#119 改动正好压测到）
- 28 次 `QueuePool timeout` 印证 #117 的诊断

用户用的是 `qwopus3.5:27b-32k`，open-ended research 型任务，信息本身稀缺，模型也在反复自我怀疑。放弃这个组合，转做 #121。

### #121 — provider_sub_kind + per-model sampling params（完整全栈）

此前 per-model 能填 temperature / top_p / top_k / presence_penalty / repetition_penalty，但几个问题：

1. OpenAI Chat Completions API 对 `top_k` / `repetition_penalty` 会 400（严格校验未知字段）；`openai_compat.py` 靠 `"api.openai.com" in base_url` 这种字符串检查来豁免，易错又不扩展。
2. 缺 Ollama 专属 `num_ctx`、OpenAI 家 `frequency_penalty`、Anthropic 扩展思考的 `thinking_budget_tokens`。
3. 用户要按 provider 类型给不同的参数集——ollama/volcengine/openai 三家互相不兼容，不能一把全给出。

#### Phase 1A — schema + migration

- `schemas/provider.py` 加 `ProviderSubKind` enum（`openai_chat` / `ollama` / `volcengine` / `anthropic`）+ `SUB_KIND_PARAM_WHITELIST` 字典。四档的允许参数集：
  - `openai_chat`: temperature / top_p / max_output_tokens / presence_penalty / frequency_penalty
  - `ollama`: temperature / top_p / top_k / max_output_tokens / repetition_penalty / num_ctx
  - `volcengine`: 同 `openai_chat`（火山引擎兼容 OpenAI 接口但需要 thinking enable，走 `extra` 携带）
  - `anthropic`: temperature / top_p / top_k / max_output_tokens / thinking_budget_tokens
- `ProviderConfig` 新加字段 `provider_sub_kind: ProviderSubKind | None`（`None` = 管理员未分类，不校验）+ `_validate_sub_kind_params` `model_validator`：sub_kind 设了就遍历 available_models，非 None 的参数必须在白名单里，否则 `ValueError`。
- `ModelInfo` 补三个字段：`frequency_penalty`、`num_ctx`、`thinking_budget_tokens`。`max_output_tokens` 名字保留原值（frontend 已有大量依赖），不做重命名。
- Alembic 迁移 `0009_provider_sub_kind_backfill.py`：`provider_sub_kind` 存 JSONB `payload`，不改列；用一条 `UPDATE` 把现存 `provider_kind='anthropic_native'` 的行 payload 里 `provider_sub_kind` 回填为 `"anthropic"`。`openai_compat` 行留 NULL——管理员在 Settings UI 手动挑一次（最初想按 base_url 猜 ollama/volcengine，用户否了："ollama可不一定是localhost:11434，这个还是得让用户自己选了"）。

#### Phase 1B — adapter wiring

- `providers/base.py::chat` 签名加三个新 kw：`frequency_penalty`、`num_ctx`、`thinking_budget_tokens`。
- `providers/openai_compat.py::__init__` 多一个 `sub_kind: str | None` 参数；`chat()` 用 `SUB_KIND_PARAM_WHITELIST[ProviderSubKind(self._sub_kind)]` 查允许集，通过 `_allowed(name)` 闭包一层过滤 payload。`sub_kind=None` 时退化为旧行为（全量发送），保证测试 double 和未迁移的历史 provider 不炸。
- 旧的 `"api.openai.com" in self.base_url` 字符串判断删掉——现在由白名单驱动，不再需要特判。
- `providers/anthropic_native.py::chat` 接 `thinking_budget_tokens`：置位时 `payload["thinking"] = {"type":"enabled","budget_tokens":N}`，并**丢掉 `temperature`**（Anthropic 要求 thinking enabled 时 temperature=1，直接不发让服务端用默认最省事）。
- `providers/registry.py::build_adapter` 多一个 `sub_kind` 参数；只在 `kind=="openai_compat"` 时透传（anthropic_native 的 sub_kind 由 adapter 类隐含）。
- `api/workflows.py::_provider_call_from_settings` 取 `config.provider_sub_kind` 传给 `build_adapter`，从 `model_info` 读三个新字段传给 `adapter.chat`。
- `api/providers.py` 的 `CreateProviderRequest` / `PatchProviderRequest` 加 `provider_sub_kind` 字段；PATCH handler 在字段变更后显式 `ProviderConfig.model_validate(config.model_dump())` 重跑一遍校验器（Pydantic v2 字段赋值默认不触发 `model_validator`），违反白名单时返回 422。
- `db/repositories/provider.py::list_all` 在轻量 summary 里暴露 `provider_sub_kind`，让 UI 识别「未分类」状态。

新增 7 个单测：
- `test_providers_openai_compat.py`：`test_sub_kind_openai_chat_drops_top_k_and_repetition_penalty` / `test_sub_kind_ollama_keeps_top_k_and_num_ctx_but_drops_frequency_penalty` / `test_sub_kind_none_is_permissive`
- `test_providers_anthropic_native.py`：`test_chat_thinking_budget_tokens_emits_thinking_block`（验证 `thinking` block + `temperature` 被丢弃）
- `test_schemas.py`：`test_provider_config_sub_kind_rejects_disallowed_param` / `test_provider_config_sub_kind_allows_whitelisted_params` / `test_provider_config_sub_kind_none_skips_validation`

#### Phase 2 — 前端 UI

- `lib/api.ts` 加 `ProviderSubKind` 类型 + `SUB_KIND_PARAM_WHITELIST` 常量（直接镜像后端）+ `ModelInfoDTO` / `ProviderSummary` / `CreateProviderBody` 三个接口新字段。
- `components/Settings.tsx::ProviderForm`：
  - 加 `subKind` state，仅在 `kind === "openai_compat"` 时渲染「子类型」下拉（openai_chat / ollama / volcengine + 未分类）。切换 kind 时自动把 `anthropic_native` 绑定到 `anthropic`，`openai_compat` 清空为 null 让用户重选。
  - 原来硬编码 5 列的采样参数网格改为白名单驱动：`SAMPLING_FIELDS` 列表里 8 个字段，根据当前 `subKind` 的白名单过滤出实际可编字段。
  - sub_kind 未选时展开采样面板显示琥珀色提示「请先选择子类型」，禁止编辑——避免写入会被后端 422 拒绝的参数。
- i18n 补 8 条键（中英双份）：`sub_kind` / `sub_kind_unset` / `sub_kind_openai_chat` / `sub_kind_ollama` / `sub_kind_volcengine` / `sub_kind_hint` / `sub_kind_required_for_sampling` + 采样 toggle hint 重写。

#### 端到端验证

- 后端 306 → 313 测试（+7）全绿。
- 前端 53/53 测试全绿；`tsc --noEmit` 干净（仅 `ConversationView.tsx:287` 一条 pre-existing 的 ChatFlowNode|null 类型错，和本轮无关）。
- 跑了一次真实 PATCH 验证：`provider_sub_kind=openai_chat` + `available_models=[{id:"x",top_k:40}]` → HTTP 422，错误信息清晰点名 `model x: param 'top_k' not allowed for provider_sub_kind=openai_chat`。
- 应用迁移 `0009` 到 dev DB 后 `/api/providers` 返回正确：Volcengine/Ollama 两行 `provider_sub_kind=null`（待用户分类），Anthropic 行自动 `anthropic`。

### 刻意没做的

- **discover_models 自动推断 sub_kind**：最初设计里想靠 `base_url` heuristic 自动填，用户否了。现在是完全手动——管理员在 Settings UI 给每个 `openai_compat` provider 挑一次就行，挑完后以前写的参数若非法会在 PATCH 时 422 暴露。
- **Anthropic native 的 thinking_budget_tokens UI**：字段和 schema 打通了，但 Settings 没有专门的「思考预算」可视化提示。ModelInfo 的白名单里它和其它数值字段一样用普通 `SamplingInput` 编辑，功能上够用。
- **Volcengine thinking enable 的 UI 开关**：目前还是 `_provider_call_from_settings` 里硬编码检测 `volces.com` / friendly_name 包含 "volcengine" 才注入 `extra={"thinking":{"type":"enabled"}}`。sub_kind 已经能识别了，但这一跳还没串起来，下一轮再改。
- **discover_models 调用**：`api/providers.py::discover_models` 和 `test_connection` 里的 `build_adapter` 也加上了 `sub_kind` 透传，但这两个场景本身不跑采样，作用仅是 wire 一致。

## 2026-04-17 (续) — Ollama usage / ChatFlow UX / 接地率熔断

用户反馈 WorkNode ribbon 上的 token 用量一直是 0，并观察到拖动**运行中**的 ChatNode 松手后位置会回弹。从这两个小 bug 起步，最后落到"planner 打转几小时不落地"的根因分析，以及一个全栈的熔断机制。

### #122 — Ollama 流式 usage=0

curl 直连 `http://localhost:11434/v1/chat/completions`，不主动 opt-in 时，Ollama 的最后一个 chunk 里根本不带 `usage` 字段，OpenAI 家和 volcengine 则必须加 `stream_options.include_usage=true` 才会在末尾追发一个空 choices + usage 的 terminal chunk。

`providers/openai_compat.py` 流式分支加一行 `payload["stream_options"] = {"include_usage": True}`；非流式分支不动（OpenAI API 会 400 于非流式请求中出现该字段）。`test_providers_openai_compat.py` 补两条：流式路径断言 body 带 `stream_options=={"include_usage":true}`，unary 路径断言没有该键。顺带给三个历史 sub_kind 测试加上 `@pytest.mark.asyncio` 装饰器——虽然 conftest `asyncio_mode=auto` 下不加也能跑，但和文件其它测试保持一致，避免以后改了模式再踩。

### #123 — 运行中 ChatNode 拖拽不保存

症状：`status=running` 的 ChatNode 拖动松手后位置回弹，completed 节点正常。

Root cause 是 React Flow 的 node 对象身份 vs. SSE 驱动的刷新之间的 race：`refreshChatFlow` → `setState({chatflow: fresh})` → 依赖 chatflow 的 effect 重建 React Flow nodes 数组 → 新 Node 对象替换旧对象 → drag state 跟随旧对象失效 → `onNodeDragStop` 没机会 fire。completed 节点因为运行中 SSE 事件少，drag 窗口没被打断所以没暴露。

1+2 双保险：
1. `isDragging` ref，drag 过程中 effect 提前 return，不覆盖 Node 数组；
2. `syncTick` state 作 effect 依赖，onNodeDragStop 里 bump 一次强制触发一次末态同步，兜住首次 drag 结束后的 SSE 事件。

ChatFlowCanvas + WorkFlowCanvas 对称改动（内层 canvas 同样吃这个 race）。

### #124 — ChatFlow 活动工作面板（ChatFlowActiveWorkPanel）

Canvas 右下角浮动新组件，列"当前实际在跑"的 WorkNode，点击 drill 进对应 ChatNode 的 workflow（含嵌套 sub_workflow）。

Filter 设计用户亲自把关（hybrid c）：`status=="running" AND (step_kind=="tool_call" OR streamingDeltas[id].length > 0)`。单看 `status=running` 会把 GPU 并发=1 时排队中的 10+ 节点全列出——实际一次只能跑一个；`streamingDeltas` 非空表明该节点已经开始吐 token，是"正在执行"最可靠的信号，而 tool_call 是 RPC 不吐 token，所以单独放进条件里。`sub_agent_delegation` 容器跳过，它的 children 作为真正的 worker 会自然上来。

点击 → 新 store action `jumpToWorkNode(chatNodeId, subPath, workNodeId)`：重建 `drillStack`（ChatNode 帧 + 一串 sub_workflow 帧）、切 `viewMode="workflow"`、设 `workflowSelectedNodeId`、清 `workflowBranchMemory`，复用现有的 drill 基础设施。

i18n 两条键 `chatflow.active_work` / `chatflow.active_work_empty`；面板在 `<ReactFlow>` 内 `<ModelRibbonLayer>` 旁 mount。

### #125 — 慢 ChatNode `019d99b4-29fa-7c32-b6ea-e8b526bdeea5` 诊断

用户跑了约 2h 46m 才失去耐心手动停止。Forensic diagnosis 从 Postgres docker container 里 dump 整棵 WorkFlow 树：

- 392 个 WorkNode、11 层 `sub_agent_delegation` 嵌套、只有 **2 个成功的 tool_call**——planner 纯粹在"拆 / 评审 / 再拆"打转，从不落到真正的动作。
- ChatNode 用的是 `gemma4:26b-64k` 本地 Ollama，**且该 ChatFlow 未设 `default_judge_model` / `default_tool_call_model` 覆盖**，所以判断/规划/主 LLM 调用全打同一个慢模型，平均单次成功 ~300s，最长 17 分钟。
- 53 次 `judge_call` + 13 次 `llm_call` 命中 `ReadTimeout`，每次卡 ~6 分钟（httpx 120s × `_MAX_RETRIES=3` + 指数退避 1+2+4s ≈ 366s）。
- `~/.agentloom/logs/backend.log` 只留 HTTP access，引擎级日志没 flush——下次排查前先修 logging。

结论：这种"深嵌套 planner 空转"的病态模式需要熔断。直接引出 #126。

### #126 — 接地率熔断（planner-grounding fuse）

每一层 engine 独立判决：完成的非 `sub_agent_delegation` 叶子（`succeeded`/`failed` 状态的 llm_call + tool_call + judge_call）中，`tool_call` 占比若低于 `min_ground_ratio` 且总数超过 `ground_ratio_grace_nodes` → 设 `workflow.pending_user_prompt`，复用现有 halt 路径把信号冒泡到 ChatFlow 层。

几个关键设计选择：

- **局部判决而非全树递归**：sub-halt bubbling 已经实现（`_run_sub_agent_delegation` 里 `sub.pending_user_prompt != None` 时 `node.error = "sub-WorkFlow halted: …"; node.status=FAILED`），每层 sub-engine 自己的 halt 会顺势冒泡。局部判决避免双重计算，语义也更清晰："这一层自己是不是在空转"。
- **排除 `sub_agent_delegation` 容器**：一个健康的 dispatching 父层可能 20 个 delegation、0 个直接 tool_call，若把容器算进总数会误判。容器不是叶子，不应被计数。
- **只数 terminal 状态叶子**：`PLANNED` / `RUNNING` 的节点不算（planner 还在"结果未知"态，不该据此下结论）。
- **默认启用**：用户明确要求开启。schema 默认 `min_ground_ratio=0.05, grace=20`——即已完成 20 个叶子后若 tool_call 占比低于 5% 就熔断。`None` 禁用。UI 把 0-1 小数转换为 0-100 百分比展示，留空 ↔ `null` ↔ 禁用。

全栈改动：

- `schemas/chatflow.py`：`ChatFlow` 加两个字段（`min_ground_ratio: float | None = 0.05`、`ground_ratio_grace_nodes: int = 20`）。
- `engine/workflow_engine.py`：`execute()` 加两个 `_UNSET`-sentinel 参数；加 module-level `_compute_ground_ratio()`；每个 batch 完后、halt 判定前调用一次；`_run_sub_agent_delegation` 把阈值透传给 sub-engine。
- `engine/judge_formatter.py`：加 `format_ground_ratio_halt_prompt(leaves, tools, min_ratio)`，仿 `format_revise_budget_halt_prompt` 风格。
- `engine/chatflow_engine.py`：`self._inner.execute(...)` 调用处 plumb 两个新参数。
- `db/repositories/chatflow.py`：`patch_metadata` 加两个 kwargs + payload 写入。
- `api/chatflows.py`：`PatchChatFlowRequest` 加字段 + kwargs 展开 + runtime mirror（注意 `min_ground_ratio=None` 是合法语义"禁用"，runtime mirror 不做 `is not None` 过滤）。
- 前端：`types/schema.ts` + `lib/api.ts` + `store/chatflowStore.ts`（optimistic 更新同样要处理 `None` 语义）+ `components/ChatFlowSettings.tsx` 加两个数字输入框 + i18n 四条键（中英）。
- 6 处老 fixture 补齐两个新字段：`sed` 一把刷过 `ConversationView.test.tsx` / `ChatFlowCanvas.test.tsx`(×2) / `chatflowStore.test.ts`(×3)。
- 新集成测 `tests/backend/integration/test_ground_ratio_halt.py` 4 条：默认禁用时允许 100% llm_call 运行、阈值触发时 `pending_user_prompt` 被设置且包含预期措辞、grace 窗口内不触发、`_compute_ground_ratio` 单元正确排除 delegation 容器和 PLANNED 节点。

最终 backend 318 passed + 4 skipped、frontend 53/53、tsc 干净（除 pre-existing `ConversationView:287`，见 #128）。

### #127 — ChatFlowSettings 三个模型下拉折进「高级」区块

Composer 侧已有 `ComposerModelPicker`（`ConversationView.tsx:472`）覆盖 llm/judge/tool_call 三 kind 的 per-turn 选择，存 `usePreferencesStore.composerModels` 跨 ChatFlow 共享。ChatFlow 层的 `default_model` / `default_judge_model` / `default_tool_call_model` 变成 composer 留"继承"时的 fallback——不同 ChatFlow 要各自兜底时仍然有用，所以不删。

改成可折叠的「高级：本 ChatFlow 的模型兜底」disclosure，默认收起，点击展开。中英文 hint 里解释层级关系（composer 是全局偏好、ChatFlow 层是 per-ChatFlow 兜底），免得半年后忘了为什么保留。i18n 两条新键 `advanced_models` / `advanced_models_hint`。

### #128 — ConversationView leafNode 类型修复

顺手扫雷。`ConversationView.tsx:169` 算出 `leafNode: ChatFlowNode | null`（`?? null`），但 `ComposerFooter` props 声明 `ChatFlowNode | undefined`，tsc 两周前就在报这个 `TS2322`。内部只用 `leafNode?.status === "running"` 可选链，运行时一直没事。一行把 props 类型改 `| null`。

### 刻意没做的

- **ChatFlowActiveWorkPanel vitest 单测**：组件依赖 store + i18n + JSX mount，写起来重；逻辑核心 `collectActiveWorkNodes` 是纯函数，未来要加单测先从它开始。
- **接地率熔断的实战回归**：单测覆盖了触发逻辑，但没跑真实 planner-stuck 场景看 halt 后的 UX（用户见到 `pending_user_prompt` 被渲染成"需要你"提示）。下次 opportunity 再做，本地跑需要先设个会打转的 setup。
- **backend engine 日志 flush 修复**：#125 诊断时发现 `backend.log` 只有 HTTP access、引擎级 log 没出来。下次排查前先修。
- **Volcengine thinking enable UI 开关**（上一轮 #121 遗留）：`_provider_call_from_settings` 里还是硬编码 URL 判断，sub_kind 能识别了但 wire 还没串到这里。

## 2026-04-18 — 一键启动脚本 + 三条小尾巴收工

回来继续开发。先补一个基础设施坑：之前只有 `Makefile` 目标，没有一键脚本，起服务要两个终端；然后把上次 defer 的小尾巴（#131 日志 flush、#132 实战回归、#133 Volcengine thinking toggle）做掉。

### #129 — `scripts/dev.sh` 一终端启动器

`make dev` 里 `uvicorn` 找不到 —— base conda 环境没装。写 `scripts/dev.sh`：activate `agentloom` conda env、`docker compose up -d postgres redis`、`alembic upgrade head`、后台起 uvicorn + vite、`tail -F` 两条 log 并加 `[be]` / `[fe]` 色彩前缀、Ctrl+C 走 `trap` 干净收尾。Makefile 加 `make up` 快捷方式。`.dev-logs/backend.log` + `.dev-logs/frontend.log` 持久化日志，为 #131 铺底。

### #130 — 插叙：原来 #132 到底是什么动机

用户问"为什么 #132 当初挂上"。回溯：#126 的接地率熔断是直接为 #125 那个 2h46m / 392 节点 / 2 个 tool_call / 手动击杀的灾难设计的保险丝；但 #126 只跑了**单测**（mock engine + 手搓 state）。没被真实验证的点：(1) 触发算法在活引擎 + SSE 流里真的会 fire 吗；(2) halt 文案在浏览器里读起来顺不顺。讨论后认识到 (2) 复用了所有 halt 类型共用的 `pending_user_prompt → isAwaitingUser` 渲染路径（`ChatFlowNodeCard:87`），风险低；(1) 合成难度高于收益，决定 defer 到自然触发。

### #131 — Backend engine 日志 flush

Root cause：`main.py` 从来没配过 `logging.basicConfig` / `dictConfig`。Python 根 logger 默认 WARNING，`log.info(...)` 一律沉默；uvicorn 只管自己的 `uvicorn.*` 树，`agentloom.*` 无人接手。

修法：新增 `_configure_logging(level_name)`，装一个 `StreamHandler` 到 `agentloom` 子树（不动 root，避免和 uvicorn 打架），读 `settings.log_level`（env `AGENTLOOM_LOG_LEVEL`，默认 INFO）。`propagate=False` 防止双写。一次性 guard `_LOGGING_CONFIGURED` 防 reload 时重复 addHandler。

顺手给 `workflow_engine.py` 补三条关键 `log.info`：
- `ground-ratio fuse halt: workflow=... leaves=N tools=M ratio=... threshold=...`
- `revise-budget halt: workflow=... revise_count=N budget=M`
- `sub-WorkFlow halt bubbling up: parent=... sub=... node=...`

下次真遇到 planner 空转，`backend.log` 就能完整记录三种 halt 的触发链。

### #132 — Ground-ratio 实战回归（deferred）

如上讨论结果。不做合成触发、等自然发生。日志 flush 已就位。

### #133 — Volcengine thinking-enable UI 开关（干净版全栈）

问题：`workflows.py:393` 用 `"volces.com" in config.base_url or "volcengine" in config.friendly_name.lower()` 硬判 + 无条件打开 thinking；用户想关都关不掉。

设计决定（讨论过"语义 overload 复用 `thinking_budget_tokens` 字段"的便宜方案，推演后否决——会让 UI 撒谎、validator 失效、注释过期、未来加 budget 语义时制造数据歧义，所以选干净版）：
- 新字段 `ModelInfo.thinking_enabled: bool | None`，三态：`None` = 跟 provider 默认（volcengine 下=ON，保留旧行为）；`True` = 强开；`False` = 强关。
- `SUB_KIND_PARAM_WHITELIST[VOLCENGINE]` 加 `thinking_enabled`，其它 sub_kind 不加——开放 anthropic 会和现有 `thinking_budget_tokens` 重复，开放 openai_chat/ollama 是假语义。
- `_validate_sub_kind_params` 的 `param_fields` tuple 加进去，validator 自动拦截 `sub_kind != volcengine` 上设 `thinking_enabled` 的误操作。

Backend 调用点重构：把 `call_extra` 的赋值**下移**到 `model_info` 解析完之后，由 `provider_sub_kind == ProviderSubKind.VOLCENGINE` 触发，`model_info.thinking_enabled` 覆盖默认 True。

Frontend：
- `api.ts` 的 TS mirror 同步字段 + 白名单。
- `Settings.tsx` 加 `ThinkingToggle` 组件（`<select>` 下拉三选一：默认 / 强开 / 强关）。不能走现有 `SamplingInput` 的数字输入路径，bool 语义差异大。渲染位置紧挨 sampling 网格下方，gate 条件 `samplingWhitelist.has("thinking_enabled")`——白名单里没这一项时（anthropic/ollama/openai_chat）自动隐藏。
- i18n 三条键 ×2 语言：`thinking_default` / `thinking_on` / `thinking_off`。

测试：`test_schemas.py` 加两条 —— volcengine 允许 + openai_chat 拒绝。backend 320 passed / frontend 53/53 / tsc 干净。API 验证 `curl /api/providers` 看到 `thinking_enabled=None` 序列化透传。

### 刻意没做的（本轮）

- **`_LOGGING_CONFIGURED` 的进程级 guard**：reload 时 Python 模块会重新 import，guard 从 False 重置，所以实际上每次 reload 会重装 handler。当前 `addHandler` 没有去重逻辑，理论上 handler 会堆积——但 `StreamHandler` 指向同一个 stderr，堆积只会写多份相同日志。如果未来发现 `[be]` 前缀后的一行重复 3 次以上，就是这个问题，到时候改成"删旧 handler 再装"。
- **Volcengine 之外的 thinking 语义统一**：Anthropic 的 thinking 由 `thinking_budget_tokens` 非 None 触发，与 Volcengine 的布尔字段语义并存。短期内没问题（validator 分别 gate），但两条路径未来合并时可以考虑引入一个 provider-adapter 层的 `resolve_thinking_mode()`。

## 故意没动的（next-step 候选清单）

- 半自动模式 UI（关键帧画布、locked/unlocked 操作）
- MCP runtime wiring（M7.5 backlog，lib 已有）
- Skill 模块 / 记忆模块（全新模块，未设计）
- redo_aggregation 路径仍 flat-format（Phase 3 surface 出来的次级 gap）
- 通知外部通道（webhook / desktop / 邮件）
- **Conversation compaction**（三层压缩：ChatFlow / WorkFlow / UI）— 报告见 `docs/research-conversation-compaction.md`，待讨论

---

## 2026-04-18 夜 — Conversation Compaction 设计定稿

与用户迭代讨论压缩系统设计，最终确定三档策略与 Tier 1 触发点。

### Merge 与 Compact 的统一

用户指出 "merge 也是一种特殊的 compact"——两者的"最近几轮"都可以用**距离输出边的拓扑距离**定义：`recency(node) = distance_to_output_edge(node)`。线性 ChatFlow 是这个定义的退化形式，DAG WorkFlow 是一般情况。后续压缩算法对两者统一处理。

### 压缩作为可复用的 plan

Compact 不是硬编码的引擎动作，而是一个**存档的 WorkFlow 模板（compact_plan.yaml）**。每次触发就基于这个 plan 实例化一个 compact WorkNode，和 judge/planner 走一样的模板实例化路径。优势：dogfooding（平台级操作复用平台自身能力）、用户可以查看/修改压缩逻辑、snapshot 有清晰的产生者。

### 双轨制：ChatFlow 触发 vs WorkFlow 触发

不把触发点全下沉到 WorkFlow 层。双轨并存：
- **ChatFlow 层显式触发**：bar 显示基于 judge_model 的 context_window（judge 是对话的 bookend），到达 trigger_threshold 时自动添加 compact ChatNode 或用户手动添加；用户能清楚看到自己的消息何时被压缩。
- **WorkFlow 层隐式触发**：每次 llm_call 前引擎做字符级 token 估算，超 70% 就**在当前节点前插一个 compact WorkNode**，压缩完成后原节点重试。静默进行。

### Bar 显示语义（讨论否决的方案）

曾提议 "ChatFlow 显示上限 = 最窄模型上下文"——用户否决："假如我使用的 tool call 模型上下文很小，但是因为它的上下文是 planner 给它的，不需要携带全部上下文也能正常完成。如果按照它的上下文来压缩就委屈其他上下文更长的模型了。" 改为：**bar 显示 = judge_model 的 context_window**（如无 judge 则 llm_call 的），因为 judge 是 ChatFlow 对话的开头和结尾。WorkFlow 内部 WorkNode 各自用自己模型的阈值独立判断，与 ChatFlow bar 解耦。

### Bar 显示维度（讨论否决的方案）

曾提议"超过最新 compact 节点之前的消息不计入 bar 占用"——用户否决（选了后者版本）："当选中它或者它的后续节点，右侧显示消息记录的时候，从这个节点压缩过的消息开始。" bar 始终按当前 ChatNode 视角的实际上下文计算；但**消息面板**在选中 compact 节点或其后裔时，只显示 compact 之后的消息——用户能直观看到"哪些消息被压缩掉了"。

### 频率控制（讨论否决的方案）

曾提议 WorkFlow 压缩加"冷却期"防止同一路径反复触发压缩——用户否决："WorkFlow 大多数节点上下文也是独立的互不干涉，所以之类不应该设置频率控制。" 按需触发，每次 llm_call 前独立判断。

### judge_post blackboard 溢出路径

用户提出疑问：judge_post 有读 sibling 节点数据的能力吗？会不会 judge_post 自己输入不超限但读 sibling 数据时超限？

查证结论：**目前没有动态读取能力**。judge_post 看到的 `layer_notes` 是引擎调用前静态拼好的（每 sibling 一行，硬截 200 char），`worknode_catalog` 也是静态列表。真正的溢出风险在 `upstream_summary` 的 `decompose_aggregation` / `redo_aggregation` 路径——它**静态拼入每个 subtask 的完整 body**，没有截断。这条路径仍落在 Tier 1（pre-llm_call 拦截）内覆盖，不需要新档位。

将来 MCP/skills 落地后，可把 "read_node_detail(node_id, field)" 包成 skill 让 judge_post 按需拉取——届时会引入新的 tool_result 溢出路径，走 Tier 0 处理。已记入项目记忆 backlog。

### 三档策略总览

| Tier | 触发点 | 作用对象 | 实现位置 |
|------|--------|----------|----------|
| 0 | tool_call 返回后 | 单次 tool_result 过大（文件读取、网页抓取） | ToolRegistry 包装层（独立，后做） |
| 1 | llm_call / judge_call 即将发起 | WorkFlow 内部上下文累积超限 | `_invoke_and_freeze` 前置检查 |
| 2 | ChatFlow turn 生成 / 用户手动 | ChatFlow 消息链累积超限 | ChatFlow 引擎 + UI 显式节点 |

### Tier 1 实现蓝图（即将落地）

锚点：`workflow_engine.py:_invoke_and_freeze()`，messages 构建完、`self._provider_call` 之前。

```python
if _needs_compact(messages, ref):
    _insert_compact_worknode(workflow, node, messages, ref)
    raise _CompactRequested  # 冒泡到 execute() 主循环，重排 DAG
```

- `_needs_compact`：字符级估算 `sum(len)/4`，阈值 = ChatFlow 设置 `compact_trigger_pct`（默认 70%）× `ref.context_window`；`context_window=None` fallback 到 32000。
- `_insert_compact_worknode`：新增 `StepKind.COMPACT` 节点，parent_ids 继承当前节点的 parent_ids，当前节点 parent_ids 改为 [compact]；当前节点状态回退到 planned。
- 用 exception 冒泡而非原地插入：`_invoke_and_freeze` 已假设"调用一定发生"，用异常回 `execute()` 让它在下一轮 pick ready nodes 时自然 pick 到新插的 compact。

### compact_plan.yaml 签名

```yaml
params_schema:
  messages:          required=true   # 待压缩消息序列
  target_tokens:     required=true   # 压缩后目标
  preserve_recent_turns: default=3
  preserve_recent_tokens: default=4000
  must_keep:         optional        # 用户必留信息
  must_drop:         optional        # 用户可丢信息
  compact_instruction: optional      # 自由指令
  source_range:      optional        # 高级：显式起止范围
```

内部是一个 llm_call worker，用 ChatFlow 设置的 `compact_model`（fallback `default_model`）。

### CompactSnapshot schema

```python
class CompactSnapshot(BaseModel):
    summary: str                        # 压缩摘要
    preserved_messages: list[WireMessage]  # 近期完整保留
    source_range: tuple[int, int]       # 源消息索引 [start, end)
    dropped_count: int
    original_tokens: int
    compacted_tokens: int
    compact_instruction: str | None
```

Descendants 读 context 时，遇到 compact ancestor 就从 `summary + preserved_messages` 开始，不再往上走。

### 落地顺序

1. Schema：`StepKind.COMPACT` + `CompactSnapshot`（纯数据，无行为）
2. Engine Tier 1：字符级 token 估算 + `_needs_compact` + `_insert_compact_worknode` + ancestor walk 识别 compact 边界
3. compact_plan.yaml 模板 + instantiate 接入
4. ChatFlow Tier 2：显式 compact ChatNode + 消息面板从 snapshot 起读
5. 设置面板（compact_trigger_pct / target_pct / model / preserve_recent_turns / require_confirmation）
6. 确认弹窗 UI（manual 触发 + auto 触发需确认时）

Tier 0 作为独立后续，单独改 ToolRegistry 返回值截断。

## 2026-04-18（续）— Compaction Tier 1 + Tier 2 落地

按前一节蓝图分六步合入。落地顺序与代码位置：

1. **Schema**（`schemas/common.py` + `schemas/workflow.py`）
   - `StepKind.COMPACT = "compact"` 新增枚举值
   - `CompactSnapshot` BaseModel，字段对齐设计稿
   - `ChatFlowNode.compact_snapshot: CompactSnapshot | None`（Tier 2 标记位）

2. **compact.yaml 模板**（`templates/fixtures/compact.yaml`，后迁入 `en-US/zh-CN/`）
   - 单 `llm_call` worker，参数签名对应 `_needs_compact` 的上下文字段
   - dogfood 理念：引擎只负责"何时触发"，"如何压缩"交给模板

3. **Tier 1（WorkFlow 层）**（`engine/workflow_engine.py`）
   - `_needs_compact()`：字符级估算 `sum(len(m.content))/4`，阈值由 `chatflow_compact_trigger_pct × context_window` 决定
   - `_insert_compact_worknode()`：在当前 WorkNode 前插入 `COMPACT` 节点；原节点 parent_ids 指向 compact；状态回退 planned
   - 通过 `_CompactRequested` 异常冒泡回 `execute()` 主循环 re-pick
   - `_build_chat_context` 识别 compact 边界，遇到 compact ancestor 就用 `summary + preserved_messages` 替换更上游历史

4. **Tier 2（ChatFlow 层）**（`engine/chatflow_engine.py` / `api/chatflows.py`）
   - `ChatFlowEngine.compact_chain(chatflow_id, parent_id, preserve_recent_turns=..., compact_instruction=...)`
   - 在 `parent_id` 下生成一个 compact ChatNode（user_message 可选=compact_instruction；agent_response=summary）
   - REST：`POST /api/chatflows/{id}/nodes/{parent_id}/compact`
   - 消息面板：`ConversationView.visiblePath` 扫描 path，找到最靠下的 compact ChatNode 并把可见范围截断到它为止（前置历史由 summary 承载）

5. **设置面板**（`components/ChatFlowSettings.tsx` + `schemas/chatflow.py`）
   - 新增 `compact_trigger_pct / compact_target_pct / compact_model / preserve_recent_turns / require_confirmation` 字段
   - UI 双向绑定：输入框支持 `k / M` 后缀（`Accept k/M suffixes in compact target-tokens input`）
   - 后端 Pydantic validator 保证 `compact_trigger_pct + compact_target_pct ≤ 1.0`（避免不收敛）

6. **确认弹窗 + 手动触发**（`components/CompactChainDialog.tsx`）
   - 右键菜单"Compact from here" + 对话框选项（compact_instruction, preserve_recent_turns）
   - 触发后自动选中新生成的 compact ChatNode，方便用户核对摘要

### 验证中发现的 3 条 bug（已修）

- **Summary 级联增长**：`_build_compact_chatnode` 之前用带 preamble 的 `_build_chat_context`，第二次 compact 会把"[Prior conversation — summarized]" 当成真实消息再次压缩，summary 越写越长。修法：`_build_chat_context(..., include_summary_preamble=False)` 给 compact worker 用，summary 只在下游渲染时注入。
- **无限 compact 循环**：聚合压缩率设太激进时（`trigger_pct=0.0001`），compact 节点刚生成立即又被下一个 turn 触发自己再 compact 自己。修法：`_spawn_turn_node` 检测 `parent_ids[0]` 已是 compact ChatNode 时跳过本轮 auto-compact（见 `project_agentloom_compact_loop_fuse.md`）。
- **32K fallback 把真实 131K 模型误判为超限**：`context_window` 以前全走 fallback 32000。新增 `engine/provider_context_cache.py`，第一次见某个 `provider:model` 时从 provider metadata 读真实值；Ark 131072 tokens / Anthropic 200K / OpenAI 分档，都能正确识别。

### Tier 1 + Tier 2 双轨触发

最终在 ChatFlow 层也加了一个"chatnode_compact_trigger_pct"（默认 0.6），而非只靠 Tier 1：
- Tier 1 作用在单个 WorkFlow 内部，但一条长对话的不同 ChatNode 可能都没超单次 llm_call 阈值，累计起来却早该压了
- Tier 2 的 auto-compact 走 `_spawn_turn_node` → 插入 compact ChatNode → 用 PendingTurn 把用户消息转发到 compact 之后
- 两档互补：WorkFlow 内部的中间产物由 Tier 1 兜底；用户视角的对话链由 Tier 2 兜底

## 2026-04-19 — Fixture 语言切换 + node_context skill + execution_mode 枚举重命名

### Fixture 语言切换

用户希望模板能按 `WorkspaceSettings.language` 切换 zh-CN / en-US。改动：

- `templates/fixtures/` → `templates/fixtures/en-US/` + `templates/fixtures/zh-CN/`
- `builtin_id` 不再全局唯一，而是 `(lang, builtin_id)` 组合
- `resolve_template(builtin_id, language=...)` 在 `templates/loader.py` 内做查找
- 所有 12 个内置模板（plan / planner / planner_judge / judge_pre/during/post / worker / worker_judge / compact / title_gen / merge / _critique_base）翻译为 zh-CN 版本

### `get_node_context` skill

Judge 等下游节点有时需要拉上游 WorkNode 的完整数据，但当时没有"按需读取"的入口。修法：

- `tools/node_context.py`：`get_node_context(node_id, [field])` 内置工具
- `db/models/node_index.py` + migration `0010_node_index.py`：为 ChatNode/WorkNode 建立反向索引表，`ChatFlowRepository` 在写路径维护
- `main.py` lifespan：应用启动时回填旧行（legacy chatflows 没有 node_index 条目）

### execution_mode 枚举重命名

旧值 `"deterministic"` / `"plan_first"` / `"free"` 改为 `"native_react"` / `"semi_auto"` / `"auto_plan"`（更贴近用户视角）。

- migration `0011_execution_mode_rename.py`：扫所有 WorkflowRow / ChatFlowRow JSONB 列，in-place 改字符串值
- 前端 `ExecutionModeSlider.tsx` 同步新枚举
- E2E 验证：后端重启后 GET 确实返回新枚举（用户手工重启 uvicorn 确认）

## 2026-04-19 夜 — Merge ChatNode 上线（MVP = 手动两节点合并）

### 背景

Compact 解决的是"同一条链太长了"。Merge 解决的是**两条 fork 出去的并行对话各自有价值，想把它们的结论汇合成一个继续往下走的起点**。典型用法：用户从同一节点 fork 两个分支尝试不同策略，双方都得出部分结论后希望后续基于"两者的合集"继续对话，而不是来回切分支。

### 架构复用（和 Compact 90% 对齐）

- `StepKind.MERGE`，`MergeSnapshot { source_ids, merge_instruction, original_tokens, merged_tokens }`
- `ChatFlowNode.merge_snapshot` + Pydantic validator 保证与 `compact_snapshot` 互斥
- `merge.yaml` 内置模板（单 llm_call，签名 `{left_summary, right_summary}`）
- `ChatFlowEngine.merge_chain(chatflow_id, left_id, right_id, merge_instruction?, model?)` 镜像 `compact_chain`
- REST `POST /api/chatflows/{id}/merge`

### 与 Compact 的关键差异：Context 停止规则

Compact 是**替换式**——`_build_chat_context` 找到最近的 compact ancestor，从它开始把上游替换成 `summary + preserved_messages`。
Merge 是**硬停式**——merge ChatNode 的 `agent_response` 本身就是合成后的回复，上游已无需渲染：

```python
# _build_chat_context 内
while current is not None:
    chain.append(current)
    node = chatflow.nodes[current]
    if node.merge_snapshot is not None:
        break  # 走到 merge 就停，merge 自己是最后一条
    current = node.parent_ids[0] if node.parent_ids else None
```

`_build_tagged_chat_context_for_compact` 也加了同样的 break，避免下一轮 compact 读到 merge 以上的旧内容。

### 交互设计：VSCode compare 式双步握手

用户规格："先右键一个文件（在这里是节点）选择 select to merge，选中的第一个节点应该有 css 动画表明它被选中。再右键另一个节点 select to merge，然后开始执行操作。如果在两步中间进行了拖动页面之外的操作则视为取消 merge。"

实现落点：

- `chatflowStore.pendingMergeFirstId: NodeId | null`，三个 action：`beginPendingMerge` / `cancelPendingMerge` / `commitMergeWith`
- `ChatFlowNodeCard` 根据 `isPendingMergeFirst` 叠一层 `ring-4 ring-violet-400 animate-pulse`（紫色脉动环）
- `ChatFlowCanvas` 把 `cancelPendingMerge` 串到：
  - 空白处点击 / 节点点击 / 节点拖动开始
  - Escape 键
  - 除了 "Merge with pending" / "Cancel pending merge" 之外的所有右键菜单项
- 右键菜单根据 `mergeState ∈ { no-pending, first-pending-self, first-pending-other }` 动态展示不同项

### ConversationView merge bubble

紫色主题的消息气泡，显示：source_ids（截短 8 位）、merge_instruction、合成后的 assistant 回复、token stats。与 compact bubble 结构对齐，使用 `data-testid={conversation-node-{id}-merge}` 便于未来 E2E 抓取。

### 测试

`tests/backend/integration/test_chatflow_merge.py` 8 条：

- 上下文停止规则（pre-merge 不可见；下游只看到合成回复）
- 端到端 merge_chain（两个分支 → merge → 第三轮 turn 只看到 MERGED_REPLY_TAG）
- 自合并拒绝（ValueError）
- 未知节点 404
- MergeSnapshot / CompactSnapshot 互斥 validator
- REST round-trip（POST merge → GET chatflow 验证 merge_snapshot 完整）
- 自合并路由返回 409
- 未知节点路由返回 404

全部 385 条后端测试 + 55 条前端测试通过。

### 未做 / 后续

- N 路合并（3+ 节点）：当前 MVP 限定两个，`source_ids: list` schema 已留好扩展空间
- 合并预览：点击 pending 节点显示"如果和 X 合并会产生什么"的 diff 视图
- Merge 触发自动 title_gen：和 compact 一样，让 title 能体现 merge 后的主题

---

## 第 N 轮 — MemoryBoard 简介基础（PR 1/3）— 2026-04-20

把 MemoryBoard 系列的地基铺上。三 PR 路线（见 `docs/design-memoryboard-brief.md`）：本 PR 只落「WorkNode 完成后自动生成简介 + 写入 `board_items` 表 + ChatFlow 层 brief_model 配置」，PR 2/3 留给读路径 skill 和 ChatBoard 继承。

### 核心落点

- **Schema**：
  - `StepKind.BRIEF`（新）+ `NodeScope { NODE, FLOW }`（新）。BRIEF WorkNode 必须带 `scope`，其它 kind 必须没有。
  - `WorkFlow.brief_model_override: ProviderModelRef | None`（新，与 `judge_model_override` / `tool_call_model_override` 并列）。
  - `ChatFlow.default_model` → `ChatFlow.draft_model`（重命名）；旧 payload 通过 `@model_validator(mode="before")` 平滑迁移。
  - `ChatFlow.brief_model: ProviderModelRef | None`（新）。`@model_validator(mode="after")` 校验 `brief_model.context_window >= draft_model.context_window`（走 `provider_context_cache`）；cache miss 时静默放行，避免启动失败。
- **持久化**：
  - `board_items` 表（alembic 0012）：`id / workspace_id / chatflow_id / workflow_id / source_node_id / source_kind / scope / description / produced_tags / consumed_tags / fallback / created_at / updated_at`；唯一键 `(workspace_id, source_node_id)` 让重跑 idempotent。（原设计里的 `forget_counter` 列在 2026-04-21 被 alembic 0017 删除——遗忘计数器改为挂在 `CompactSnapshot.sticky_restored` 上按源节点计数。）
  - `BoardItemRepository`（workspace-scoped，ADR-015）+ `upsert_by_source` 一把事务。
- **引擎 `_run_brief`**：
  - tool_call 源走确定性 code template（`tool_call <name>[ error]: <first line>`），不进 provider——ReAct 循环里每个 shell 输出都跑一次 LLM 会烧钱。
  - 其它 kind 用 `node_brief.yaml` / `flow_brief.yaml` 拼 prompt，走 LLM；失败（provider 挂、reply 空）落到同一个 code template，`fallback=True` 打标。
  - brief 本身永远 SUCCEEDED——从不把自己的失败抛给 source WorkNode。
- **自动挂载**：
  - 每个非 BRIEF、非 SUB_AGENT_DELEGATION 的 WorkNode 进 SUCCEEDED 后，引擎自动挂一个 `scope=NODE` 的 brief，`parent_ids=[source.id]`。
  - WorkFlow 终态（succeeded / halted / cancelled 任意一种）同步挂一个 `scope=FLOW` 的 brief，parents 是所有终态 main-axis 节点；`pending_user_prompt` 文本进 `terminal_output` 让 halt 也有 brief。
  - 递归守卫：brief 不再被 brief；delegate 不生 node-brief（它的 sub-WorkFlow 自己有 flow-brief）。
- **Opt-in 闸门**：两条挂载路径都要求 `workflow.brief_model_override is not None`。用户不设 `brief_model` 就等于关闭 MemoryBoard 写入——全盘零额外开销。这是为了不破坏现有两百多条 scripted-provider 测试——它们按 reply 数精确计数，如果默认开启 brief 会全线爆。设计文档里 brief_model 本来就是「pin」，这里把「未 pin 视为关闭」明确写进引擎。
- **Fixtures**：`templates/fixtures/{en-US,zh-CN}/{node_brief,flow_brief}.yaml` 各一份。两份 yaml 各是单节点 ReAct plan，system prompt 约束「1-2 句散文、不要 markdown、保留 tool name / file path / id / error name 这类具体锚点」。
- **ChatFlowEngine 胶水**：
  - `_make_board_writer(tool_context)` 返回一个 closure：拿 `get_session_maker()` 开会话，工作区从 `ToolContext` 继承（缺省走 `DEFAULT_WORKSPACE_ID`），调 `upsert_by_source` 然后 commit；全程捕获异常写 `log.exception`，确保 brief 写失败不影响主流程。
  - 每个 `self._inner.execute(...)` 站点都传 `chatflow_id=chatflow.id`；writer 在 `chatflow_id is None` 时静默跳过（`_precompact_branch_for_merge` 那个站点没有 chatflow 上下文，就走这条路）。

### 前端

- `types/schema.ts`：`default_model` → `draft_model`，新增 `brief_model`。
- `store/chatflowStore.ts` / `lib/api.ts` / `canvas/*`：同步改名 + 新增 `brief_model` 字段。
- `components/ChatFlowSettings.tsx`：tool_call_model 下方新增 brief_model 下拉，默认 `(disabled — no MemoryBoard)`。
- `i18n/{en-US,zh-CN}.json`：Default model → Draft model（中文「草稿模型」）、新增 brief_model / brief_model_hint / brief_model_disabled。
- 测试夹具（`store/chatflowStore.test.ts` / `canvas/ChatFlowCanvas.test.tsx` / `canvas/ConversationView.test.tsx`）统一换字段。

### 测试

后端：`tests/backend/unit/test_memoryboard_brief.py` 10 条：

1. `test_brief_scope_validator` — BRIEF 必须带 scope；非 BRIEF 不得带 scope。
2. `test_draft_model_rename` — 旧 key `default_model` 通过后仍落到 `draft_model`；双键并存时 `draft_model` 赢。
3. `test_brief_model_context_window_invariant` — `brief_cw >= draft_cw` 通过；小于抛错；未知模型静默。
4. `test_brief_spawn_on_success` — `brief_model_override` 开关下，单节点 workflow 跑完后有 1 个 node-brief + 1 个 flow-brief，BoardItem 写了两行。
5. `test_brief_recursion_guard` — brief 不再挂 brief。
6. `test_brief_tool_call_code_template` — tool_call 源 brief **零 provider 调用**，输出以 `tool_call shell:` 开头，`fallback=True`。
7. `test_brief_llm_failure_fallback` — provider raise 时，brief 仍 SUCCEEDED，用 code template，`fallback=True`。
8. `test_brief_flow_scope_spawn` — flow-brief 只跑一次，BoardItem 的 `source_node_id == workflow.id`、`source_kind="flow"`。
9. `test_brief_flow_spawn_on_halt` — 预置 `pending_user_prompt` 也生 flow-brief（halt 故事最值得写入 MemoryBoard）。
10. `test_brief_delegate_has_no_node_brief` — delegate WorkNode 不长 node-brief；外层 flow-brief 仍在。

全量跑（忽略 3 份 LCA-merge stash 遗留的损坏文件 `test_citation_fallback.py` / `test_merge_context.py` / `test_chatflow_merge.py`）：**387 passed, 4 skipped**。前端 `npx vitest run`：**55 passed**。

### 坑与决策

- 第一次跑全量测试有 35 条失败，排查：`_make_board_writer` 因为测试里 chatflow 没落库，`board_items.chatflow_id` FK 撞红线——但 writer 自己已有 try/except 吞掉。真正的问题是**briefs 占用了 scripted-provider 的 reply 计数**，第二轮 turn 的主 llm_call 拿不到回复而 FAIL。最终方案：把 brief 挂载改成「opt-in by brief_model」，既不破坏既有测试，也没违背设计文档（brief_model 本来就是 pin 字段）。
- alembic 迁移 0012：`board_items` 的 produced_tags / consumed_tags 列用 `JSONB().with_variant(JSON(), "sqlite")` 保证 SQLite 测试库能跑；生产跑 Postgres。
- `_compute_ground_ratio` 显式跳过 `StepKind.BRIEF`——brief 是 off-axis 节点，不该扰动 planner-grounding fuse。

### 未做 / 后续

- **PR 2**：MemoryBoard 读路径——新增一个 skill/tool 让 agent 按需拉 BoardItem 描述（类似 `get_node_context` 但只读 brief）。
- **PR 3**：ChatBoard 级联——ChatNode 向上游走时带上祖先 brief 的摘要视图，让对话压缩不必每次都走 compact。
- Forget counter：PR 1.2（2026-04-21）把它从 `board_items` 搬到 `CompactSnapshot.sticky_restored: dict[source_node_id, int]`——粘滞在压缩快照上按源节点计数，衰减到 0 才移出 context。后续 PR 再接 `get_node_context` 调用作为 used-signal + 每轮衰减 + merge 时两边取 MAX。

---

## 第 N+1 轮 — MemoryBoard 读路径 + 检索 DDL + 简介气泡（PR 2/3）— 2026-04-20

承接 PR 1。这一轮把「读」和「看」两条路径补齐：一个给 agent 用的只读 skill、一份 Postgres 检索索引、以及画布上用户侧的 brief 可视化。

### P0 — `memoryboard_lookup` skill

- `backend/agentloom/tools/memoryboard_lookup.py`：新 `MemoryBoardLookupTool`，入参 `chatflow_id` / `workflow_id` / `scope` / `source_node_id` / `query` / `limit`，返回 `{"items": [...], "truncated": bool}`。自己 `get_session_maker()` 开会话，`limit + 1` 预取来判断截断。
- 与既有的 `get_node_context` **共存**（不删）：前者按 id 拉完整对话上下文，后者按过滤条件拉 brief 摘要。
- `backend/agentloom/tools/registry.py`：`default_registry()` 加注册，总数 7 → 8。

### P1 — Postgres 检索 DDL（Alembic 0013）

- `backend/alembic/versions/0013_board_items_retrieval.py`：Postgres-only 迁移，SQLite 直接 return。
- `board_items.description_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', description)) STORED` + `ix_board_items_description_tsv` GIN。选 `'simple'` 而非语言分词器——中文没有空白分词，`'simple'` 最兼容。
- `pg_trgm` 扩展 + `ix_board_items_description_trgm`（`gin_trgm_ops`）给模糊匹配。
- `pgvector` 扩展 + `description_embedding vector(1536)` + `ivfflat` 索引（`lists=100`）给 PR 4 的 embedding 召回预留。
- **两个扩展都是 opt-in**：先查 `pg_available_extensions`，没装就跳过。踩的坑：原本用 `try/except` 包 `CREATE EXTENSION vector`，但 Postgres 的 DDL 是事务性的——`ERROR` 之后整个事务进 aborted 状态，后续 `UPDATE alembic_version` 也失败。改成 pre-check 才干净。

### P2 — 前端 brief 可视化

- `GET /api/chatflows/{id}/board_items`：返回 `{"items": [...]}` 展平列表（`backend/agentloom/api/chatflows.py`）。
- `types/schema.ts`：新增 `BoardItem` interface。
- `lib/api.ts` + `store/chatflowStore.ts`：`boardItems: Record<NodeId, BoardItem>`（按 `source_node_id` 索引），`refreshBoardItems` 在 `loadChatFlow` 和 SSE reconcile (`_doRefreshOnce`) 里调；fail-open，任何异常只打 log 不中断。
- `canvas/nodes/WorkFlowNodeCard.tsx`：新 `NodeBriefBubble`——在 WorkNode 上方 8px 绝对定位的 chip，展示描述前 80 字符，点击展开全文。`scope === "node"` 才渲染；**递归守卫**：brief WorkNode 自己不长气泡（step_kind === "brief" 跳过）。`data-fallback` 透传让 style 可以区分 code-template fallback vs LLM-generated。
- `canvas/WorkFlowCanvas.tsx`：新 `FlowBriefBanner`——画布顶部 `scope === "flow"` banner，key 是 `workflow.id`。

### 测试

后端：`tests/backend/integration/test_memoryboard_lookup.py` 9 条：

1. `test_lookup_by_source_node_id` — 直接按 id 定位。
2. `test_lookup_by_scope` — `scope=node` / `scope=flow` 过滤。
3. `test_lookup_query_substring` — `query` 做 description ilike 匹配。
4. `test_lookup_returns_empty_list` — 无命中时 `items=[]` 不抛错。
5. `test_lookup_cross_workspace_isolation` — workspace A 看不到 workspace B 的 brief（ADR-015 validator）。
6. `test_lookup_missing_chatflow_raises` — 未知 chatflow_id 触发 ToolError。
7. `test_lookup_invalid_scope_raises` — 非 `node`/`flow`/`chat` 的 scope 值拒掉。
8. `test_lookup_limit_truncation` — `limit=N` 时 `truncated=true` 当 match > N。
9. `test_lookup_via_registry_envelope` — `default_registry().execute("memoryboard_lookup", ...)` 走完 schema 校验 + result shape。

单测侧：`tests/backend/unit/test_tools.py` 里 `test_default_registry_has_seven_tools` 改名 `..._eight_tools`，expected list 加 `"memoryboard_lookup"`。

前端：`frontend/src/canvas/nodes/WorkFlowNodeCard.test.tsx` 加 4 条——bubble 正常渲染、无 BoardItem 时不渲染、brief 节点上不渲染（递归守卫）、`fallback` 透传为 `data-fallback`。

全量跑（继续忽略 PR 1 里点名的 3 份 LCA-merge stash 损坏文件 `test_citation_fallback.py` / `test_merge_context.py` / `test_chatflow_merge.py`）：后端 **396 passed**（+9），前端 `npx vitest run`：**59 passed**（+4）。

### 坑与决策

- 集成测试最初撞 FK：`board_items.chatflow_id` → `chatflows.id`。写了 `_ensure_chatflow(session, chatflow_id, workspace_id)` helper 先插一条最小 ChatFlowRow，fixture 用 `patched_session_maker` 绑到测试 session。
- `refreshBoardItems` 失败不抛：既有 chatflowStore 测试对 fetch mock 用通用 `{}`，如果 refresh 抛会连累不相关测试。写成 try/catch + `console.warn`。
- `scope === "chat"` 的 ChatBoardItem 先不在画布上画——那是 PR 3 的 ChatNode 级继承故事；数据模型先收，UI 留给下一轮。

### 未做 / 后续

- **PR 3**：ChatBoard 级联——ChatNode 向上游走时沿祖先链继承 brief 摘要，用于对话压缩决策。
- **PR 4（延后）**：blackboard/CompactSnapshot/MergeSnapshot 迁移、`capabilities` / `assigned_resources` 字段、`compact`→`compress`、`sub_agent_delegation`→`delegate`、`llm_call`→`draft`、`planner`→`plan`——这批是语义重命名/重构，与本轮读路径解耦。
- 写 embedding 到 `description_embedding`：DDL 已建，但没有写入器；等 PR 4 接 embedding provider。

---

## 第 N+2 轮 — ChatBoard 每 ChatNode 级联继承（PR 3/3）— 2026-04-20

收尾 MemoryBoard 三 PR 系列：把 `board_items` 表从「只装 WorkBoard 行」升级为「同时承载 ChatBoard 行」的统一存储。每个 ChatNode 到达 SUCCEEDED 时同步生成一条 `scope='chat'` 的行，下游 ChatNode 要级联查祖先的 brief 时直接走 `GET /api/chatflows/{id}/board_items` + 客户端按 `ChatFlow.ancestors(node_id)` 过滤即可（设计文 §8 给过 server-side endpoint 的备选，实测客户端一行 `.filter(id => ancestors.has(id))` 够用，就没再加新端点）。

### 工程改动

- `backend/agentloom/engine/chatflow_engine.py`：
  - 新增三个 module 级 helper：
    - `_chat_board_source_kind(node)` — 根据 `compact_snapshot` / `merge_snapshot` 选 `chat_compact` / `chat_merge` / `chat_turn`。
    - `_chat_board_description(node)` — 确定性 code template：turn 走 `user asked: <120 chars>; agent: <200 chars>`，compact 写 `compacted N messages into a summary (+M preserved verbatim): <summary>`，merge 写 `merged branches <id8>+<id8> into one reply: <reply>`。纯代码模板，不走 LLM——PR 1 的 tool_call 也是一样策略，成本可预期，后续 PR 可以升级到 LLM 生成简介而无需改调用方。
    - `_first_line(text)` — 小工具，抓第一非空行。
  - `ChatFlowEngine._spawn_chat_board_item(chatflow, node)`：复用 `self._inner._board_writer`（PR 1 搭的那套 session-per-write writer），写 `scope='chat'` 行；幂等由底层 `BoardItemRepository.upsert_by_source` 保证；writer 失败吞异常只打 log，ChatNode 自身状态不受影响；`user_message is None && agent_response 空` 的 greeting root 跳过。
  - 两个接入点：
    1. 主 turn 完成路径（`_run_chat_node` 尾部）：在 `consumed_pending_id` 解析 future 之后、`cascade` 判断之前调用（覆盖普通 turn + Tier 2 compact ChatNode——后者靠 `compact_snapshot != None` 走 `chat_compact` 分支）。
    2. 合并 ChatNode 完成路径（`merge_node` finalize 之后、SSE 发事件之前）：走 `chat_merge` 分支。
- 前端：
  - `frontend/src/canvas/nodes/ChatFlowNodeCard.tsx`：从 store 读 `boardItems[node.id]`，`scope === "chat"` 才渲染一个小的 indigo 徽章（label 走 i18n `chatflow.chatboard_badge`），hover tooltip 展示完整描述。刻意做最小：WorkBoard bubble 已经是 PR 2 的视觉重头戏，ChatBoard 这里只要「存在性可见」即可。
  - `frontend/src/i18n/locales/{en-US,zh-CN}.json`：加 `chatflow.chatboard_badge` + `chatflow.chatboard_badge_hint` 两个 key。
  - `frontend/src/canvas/ChatFlowCanvas.test.tsx`：新 describe `ChatFlowNodeCard — ChatBoard badge (PR 3)` 3 条——有 `scope='chat'` BoardItem 时渲染徽章 + `data-source-kind` 正确 + tooltip 带描述；无 BoardItem 不渲染；`scope='node'`（WorkBoard 行错投到 ChatNode id）也不渲染。

### 测试

`tests/backend/unit/test_chatboard_cascade.py` 9 条：

1. `test_chatboard_item_written_on_chatnode_success` — 普通 turn ChatNode 触发 `_spawn_chat_board_item` 后 writer 拿到 `source_kind='chat_turn'` + `scope='chat'` + `workflow_id=None` + 描述包含 user/agent 文本。
2. `test_chatboard_item_idempotent` — 同一节点连调两次，两次 payload 完全相等（repo 层的 upsert 负责真正的去重；engine 层只验不自作主张）。
3. `test_chatboard_item_for_compact_chatnode` — compact ChatNode（`compact_snapshot` 置位、`dropped_count=5`）写出 `chat_compact` + 描述含「compacted 5 messages」。
4. `test_chatboard_item_for_merge_chatnode` — merge ChatNode（两 parent、`merge_snapshot` 置位）写出 `chat_merge` + 描述含两边 id 前 8 字符 + 合并回复首行。
5. `test_chatboard_ancestor_cascade` — root→A→B→C 四层链，每层都 spawn（root 被 greeting-root 跳过规则过滤掉），`cf.ancestors(c.id)` 交集 captured 行集合 = {A, B}，验证客户端级联过滤一行搞定。
6. `test_chat_board_description_chat_turn_shape` — 纯模板：`user asked: <user>; agent: <agent>`。
7. `test_chat_board_description_truncates_long_inputs` — 500 字符 user/agent 被截到上限（120/200 字符）。
8. `test_chat_board_description_empty_turn` — 空 turn 返回 `"(empty turn)"` 占位，不抛不空字符串。
9. `test_spawn_chat_board_item_skips_greeting_root` — 只有 greeting root（user/agent 都空）时 writer 不被调用。

全量跑（继续忽略 `test_citation_fallback.py` / `test_merge_context.py` / `test_chatflow_merge.py`）：后端 **405 passed**（+9，从 PR 2 的 396 起）；前端 `npx vitest run`：**62 passed**（+3，从 PR 2 的 59 起）。

### 坑与决策

- 接入点放在「ChatNode 状态 SUCCEEDED 之后、queue cascade 之前」而非「SSE publish 之后」：这样如果 board 写失败吞异常，也不影响后续 future 解析和 queue 走扁平化。
- 不做 server-side `ancestor_ids`-过滤 endpoint：设计文 §8 的备选方案，评估后决定客户端一行 `.filter` 更简单（前端已有完整 ChatFlow 在 Zustand store），避免 backend 多一个路径要维护。要是之后 item 数上千就再考虑分页 + server filter。
- 不走 LLM 写 chat brief：PR 3 明确说 code template MVP 即可；`user asked: …; agent: …` 信息密度够下游做粗筛，LLM 精炼放下一轮。
- merge 路径的 spawn 放在 runtime lock **外**：保持和 WorkBoard writer 一致的「DB I/O 别占用 chatflow 锁」策略，免得 board 写慢连累排队轮次。

### 收束：三 PR 系列是否闭合

按设计文 `docs/design-memoryboard-brief.md` §8–§10 比对：

- PR 1（brief WorkNode + draft_model 重命名 + BoardItem 表）— 已落。
- PR 2（reader skill + Postgres 检索 DDL + 前端 bubble/banner）— 已落。
- PR 3（ChatBoard 级联继承）— 已落：auto-spawn 挂接普通 turn / compact / merge 三类 ChatNode、`scope='chat'` 行写入、前端徽章可见、祖先链查询客户端化。

§10 out-of-scope 的 5 项（`pack` kind / 多索引检索 / 三级细节 / judge 打回标签 / MemoryBoard 编辑 UI）本来就不在这轮范围，符合预期。§8 deferred to PR 2 的 7 项和 §9 deferred to PR 3 的 1 项都交付了。干净闭合，不带 TODO。


## 第 N+3 轮 — MemoryBoard PR 4 系列：命名重命名 + 历史快照迁移 — 2026-04-20

设计文 §8 把 `blackboard` / `CompactSnapshot` / `MergeSnapshot` 迁移、Layer-2 枚举重命名、`capabilities`/`assigned_resources` 字段和 embedding 写入一起推到 PR 4。本轮把**前三项 + 枚举**全部收敛，PR 4.1 / 4.2 / 4.3 一共 6 个提交落在 main。`capabilities` 和 embedding writer **显式延后**——它们不是快照迁移的依赖，也不阻塞 LCA-merge 下一步。

### PR 4.1（e961276）— Layer-2 枚举重命名

一次性把 StepKind / WorkNodeRole 的对外 token 统一到 §3.4 命名表：

```
StepKind.LLM_CALL              -> DRAFT
StepKind.SUB_AGENT_DELEGATION  -> DELEGATE
StepKind.COMPACT               -> COMPRESS
WorkNodeRole.PLANNER           -> PLAN
WorkNodeRole.PLANNER_JUDGE     -> PLAN_JUDGE
```

改面：schemas 枚举 + 引擎/fixtures/前端 mirror + KIND_ACCENT/STEP_KIND_COLOR + i18n role keys + Alembic 0014（chatflows / workflows / workflow_templates 的 JSONB 文本替换 + `board_items.source_kind`）。后续 9c14f7b 修了一个 `plan/planner` fixture builtin_id 碰撞的小尾巴。

### PR 4.2.a（52a0dd4）— CompactSnapshot 直写 BoardItem

`_run_compact` 结束时直接 `_persist_board_item(scope=NODE, description=summary)`，不再额外 spawn 一个 BRIEF WorkNode。语义上 compact summary **就是**一个节点级 brief，再套一层 LLM 精炼是重复劳动，还多一跳 $。代价：brief 描述就是 summary 原文，不经 "压成三句话" 的打磨。

### PR 4.2.c（b54c7e4）— blackboard 下线，post-check 读 briefs

`judge_post` 之前从独立 `blackboard` 结构里拉"工作区笔记"；现在直接检索 BoardItems 中的 flow-brief + node-brief。删的代码：blackboard schema + read/write helpers + 相关 reader skill 分支。MemoryBoard 接手了语义，blackboard 只是临时容器。

### PR 4.3（db04a11）— 历史 ChatFlow 数据 wipe

4.3.a/b 要改 ChatFlowNode 的快照 schema。本地 + staging 的旧数据里带着即将被删除的 `MergeSnapshot` / 五个 `CompactSnapshot` 字段，与其写兼容 loader 只读、不读，不如直接 wipe——单人项目 + pre-alpha 窗口，没有用户数据要照顾。

### PR 4.3.a（dcf776d）— MergeSnapshot 整体删除

`len(node.parent_ids) >= 2` **结构上**就是 merge ChatNode 的判据，再维护一个带 token 计数 + 预压缩标记的 metadata 类是第二个真相源。`_chat_board_source_kind` / `_build_chat_context` / `_build_tagged_chat_context_for_compact` 的 boundary check 全部切到 parent_ids 长度。UI 损失：merge bubble 不再显示 "X → Y tokens" / 预压缩徽章；来源列表改从 `node.parent_ids` 读、指令改从 `node.user_message.text` 读。交换：少一个类 + 一个 validator + 三个 i18n key。

### PR 4.3.b（e8a0184）— CompactSnapshot 瘦身到两个字段

砍掉 `source_range` / `dropped_count` / `original_tokens` / `compacted_tokens` / `compact_instruction`，只留 `summary` + `preserved_messages`。砍的字段只服务 UI stats ribbon 和两行 log，reader 真正依赖的就是 summary 原文 + verbatim tail。`compact_instruction` 作为**引擎参数**留着（还要喂 compact worker 的 LLM 模板），只是不再存进快照；用户还能在 ChatNode 的 `user_message.text` 里看到自己敲进去的指令。UI 损失：compact bubble 不再显示 stats ribbon 和独立的 instruction label。

`_chat_board_description` 的 chat_compact 文案从 "compacted N messages" 改成 "compacted prior chain"——N 是被砍的 `dropped_count`，改用更笼统但仍有 `+N preserved verbatim` 信息量的描述。

### 数据兼容

Pydantic 默认 `extra="ignore"`，schemas/workflow.py 和 schemas/chatflow.py 没有 override——旧 JSONB payload 里残留的 `source_range` / `compact_instruction` 等字段会被安静丢弃，不会抛 `ValidationError`。加上 db04a11 已经 wipe 掉本地/staging 旧数据，这一路没有显性迁移成本。

### 测试

- 后端 unit：266 passed（忽略预存损坏的 `test_citation_fallback.py` / `test_merge_context.py`）。
- 后端 integration（不含 `test_chatflow_merge.py`）：141 passed。
- 后端 `test_chatflow_merge.py`：5 failed（4 个 `/merge/preview` endpoint 未实现 404 + 1 个 `board_items.chatflow_id` FK 违反——in-memory chatflow 没落库但 PR 3 writer 试图插 board_items。stash 回 PR 4.3.a 前的 baseline 跑同样复现，确认**预存非回归**）。
- 前端 vitest：62 passed。

### 坑与决策

- **枚举重命名必须带 Alembic 数据迁移**：StepKind / WorkNodeRole 存在 JSONB payload 里，不是独立列；drop 了 Python enum 但没重写 JSONB 文本的话，旧行 load 会炸。Alembic 0014 的重头戏是 `UPDATE ... jsonb_set` 扫全表。
- **CompactSnapshot 没彻底干掉，只瘦身**：`preserved_messages` 是真实运行时依赖（`_build_chat_context` 在 compact cutoff 之后要原样拼回这段 verbatim 消息），不是 metadata。硬推成 BoardItem 扩展字段要折腾 reader，收益不抵。留一个两字段的轻类比继续拆干净更务实。
- **MergeSnapshot 能整体删**：merge 判据在结构里（多父），metadata 只服务 UI，UI 损失可接受；和 CompactSnapshot 的"半留"是不对称的。
- **`compact_instruction` 的两条命**：engine 参数留（喂 LLM 模板），snapshot 字段删，显示改走 `user_message.text`——这条设计在 merge path 上也适用（`node.user_message.text` 承载用户输入的 merge 指令）。
- **数据 wipe 先于 schema 改**：本来想写 loader 兼容，但 pre-alpha 单人项目场景下 wipe 比 compat code 更快更干净。后面有真实用户数据就得反过来——先写读兼容再改写路径。
- **FK 违反不是我引入的**：`test_merge_chain_produces_merge_chatnode_and_downstream_sees_summary` 在 baseline 就挂，PR 3 ChatBoard writer 对 in-memory chatflow 的假设与 `board_items` 的 FK 约束冲突，测试 harness 缺一个"写前确保 chatflow 已落库" step。列为后续 bugfix，不阻塞 PR 4 收口。

### 未做 / 后续

- **`capabilities` / `assigned_resources` 字段**（设计文 §8 残项）：judge_pre 产出能力清单、plan 消费、worker spawn 按 assigned slice 裁上下文——这是一个 _新功能_，不是迁移。需要单独一轮设计对齐。
- **Embedding writer**：PR 2 Alembic 0013 已建 `description_embedding vector(1536)` + `ivfflat`，writer 路径 + embedding provider 选型（Ollama nomic-embed-text / volcengine / OpenAI）还没接。写入时机（同步 vs 后台 backfill）也没决。
- **`test_chatflow_merge.py` 的 5 个 failure**：4 个 preview endpoint + 1 个 FK，都是 pre-existing；merge preview 端点本身是下一步 LCA-merge 工作的入口，一并重做。
- **UI 侧验证**：merge bubble 少了 stats / 预压缩徽章，compact bubble 少了 stats ribbon / instruction label。代码面能过 vitest，但 "用户真打开这几个节点看到的体验是什么" 需要跑一次前端，本轮没做。



## 2026-04-21 夜 — MemoryBoard 粘滞遗忘四步落地

接 PR 1.2 的 forget-counter 搬家工作：原设计是把 `board_items.forget_counter` 列搬到 `CompactSnapshot.sticky_restored`，但实现到 PR 4 才发现**粘滞状态应该挂在 ChatFlowNode 本身，不是 CompactSnapshot**——因为 fork 分支应该独立演化、merge 点应该 MAX 合并，而 CompactSnapshot 是共享的。最终 four-PR 切片：

### PR 1（689f822 / 97e4e7b / fa361f0）— schema + UI 铺垫

1.1: 前端 `compact_preserve_turns` label 改为 "memory capacity n"；保留翻译两版。
1.2: `CompactSnapshot.sticky_restored: dict[str, int]` 新字段 + alembic 0017 删 `board_items.forget_counter`。
1.3: `get_node_context` 返回值加上 `parent_ids`，让 agent 能逐跳往上游走。

### PR 2（9fcfe6a）— accessed-signal

`ToolContext.accessed_node_ids: set[str]` + `agentloom.tools.base.record_accessed_node_id(ctx, node_id)` + `accessed_scope()` 上下文管理器（`contextvars.ContextVar`）。`GetNodeContextTool.execute` 命中成功时调用 `record_accessed_node_id(ctx, node_id)`，ToolError 路径不写。

关键决策：为什么要 contextvars？同一个 `ChatFlowEngine._inner._tool_ctx` 在并发 sibling ChatNode 之间共享（fork 分支各自跑 pending turn），如果直接 mutate `ctx.accessed_node_ids` 两个兄弟会串着写。contextvars 让每个 `_execute_node` 开 `accessed_scope()` 绑一个 task-local set，工具查 scope，scope 为空才回退到 ctx 字段（保留 PR2 单元测试走裸 ToolContext 的路径）。

### PR 3（907d55b）— sticky restore + 每轮衰减

`_build_chat_context` 在 summary preamble 和 preserved tail 之间插入 `_render_sticky_restored_messages` 产出的 `[Restored context for chat node <id>]` 用户/助手对。`_execute_node` 在 turn 结束后（SUCCEEDED 且非 compact 节点）调 `_update_sticky_restored_on_chain`：沿主父链找最近 compact cutoff，在那个 CompactSnapshot 上 refresh 当轮 accessed ChatNode 到 `counter_init`，未触碰条目计数减 1，减到 0 drop。`counter_init = chatflow.compact_preserve_recent_turns`（PR 1.1 重命名的 "memory capacity n"）。

10 条 unit（`tests/backend/unit/test_sticky_restored.py`）：no-op / seed / decay / drop / refresh / worknode 过滤 / header 渲染 / 计数降序排序 / 未知 id skip / `_build_chat_context` 顺序是 summary → sticky → preserved。全绿。

### PR 4（本 PR）— fork 独立 + merge MAX 搬家

**设计换向**：PR 3 的 sticky 住在 CompactSnapshot 上，fork 分支两边共享同一个字典——A 分支 `get_node_context(X)` 会让 B 分支也看到 X 粘滞，违背"两条支线各自探索"的语义。挪到 ChatFlowNode：每个节点持有自己的 `sticky_restored: dict[str, int]`，fork spawn 时从主父拷一份（`dict(parent.sticky_restored)`，不是引用），之后各自演化；merge ChatNode 构造时 MAX-merge 所有父节点的字典（新 helper `_merge_sticky_restored`）。

主要改动：
- `schemas/chatflow.py`：`ChatFlowNode.sticky_restored: dict[str, int] = Field(default_factory=dict)`。
- `schemas/workflow.py`：从 `CompactSnapshot` 撤掉 `sticky_restored` 字段。
- `engine/chatflow_engine.py`：
  - `_has_compact_ancestor(chatflow, parent_ids)` 替换旧的 `_find_compact_cutoff_id`——不再需要返回 cutoff id，只要知道"有没有"。
  - `_update_sticky_restored_for_node(chatflow, chat_node, accessed_ids, counter_init)`（重命名自 `_update_sticky_restored_on_chain`）：直接 mutate `chat_node.sticky_restored`，无 `model_copy` 因为 ChatFlowNode 是普通 Pydantic。
  - `_merge_sticky_restored(sources)`：MAX 合并多个父字典；空输入→空；单源→拷贝（mutate 不影响源）。
  - `_spawn_turn_node`：`inherited_sticky = dict(parent.sticky_restored)` 传到 `ChatFlowNode(sticky_restored=...)`。
  - `_build_chat_context`：sticky 查询从 `snap.sticky_restored` 换成 `chatflow.nodes[parent_ids[0]].sticky_restored`——每个节点看自己的粘滞，不看 compact 的。
  - merge ChatNode 构造（非 joint-compact 路径）：`sticky_restored=_merge_sticky_restored([left.sticky_restored, right.sticky_restored])`。
  - joint-compact compact_node 构造：`sticky_restored={}` 显式 reset（新 cutoff 重新开始）。
- `tools/base.py`：docstring 跟改；行为不变。

**测试**：`test_sticky_restored.py` 扩到 14 条，新加 `test_forked_siblings_evolve_independently` / `test_merge_takes_max_of_parent_sticky` / `test_merge_empty_sources` / `test_merge_single_source_copies`。全量后端 `pytest tests/backend/ --ignore=test_citation_fallback.py --ignore=test_merge_context.py`：**443 passed, 4 skipped**。

### 坑与决策

- **为什么 sticky 搬到 ChatFlowNode 而不是留在 CompactSnapshot**：fork 语义要求状态 per-node；`model_copy(update=…)` 每轮重建 CompactSnapshot 也不够 ergonomic。ChatFlowNode 本身是可变 Pydantic，sticky 就是它的运行时状态，天然贴合。
- **joint-compact 为什么 reset 而不是继承**：joint-compact 是一个新的 cutoff，语义上等价于一次 "重新开始记忆"——下游看到的就是这个 joint 的 summary，之前的粘滞都归零。如果要保留需要额外设计（例如在 joint-compact 时也 MAX-merge 两个源的 sticky），但收益不显著（joint-compact 本身已经把两边的内容注入了 summary），暂时保守。
- **merge 节点是构造路径不是 spawn 路径**：merge ChatNode 通过 `ChatFlowNode(...)` 直接构造而不是走 `_spawn_turn_node`，所以 inherited_sticky 的逻辑不经过。改成显式计算 `merged_sticky` 然后传参。joint-compact 路径的 merge 只有单父（compact_node），MAX 退化为拷贝 {}。
- **contextvars 回退路径保留**：PR 2 的 bare `ToolContext` 单测路径（不开 `accessed_scope`）继续走 `ctx.accessed_node_ids.add(...)`，保留为兼容面。引擎路径一定走 scope。
- **worknode id 双过滤**：update 时 `{nid for nid in accessed if nid in chatflow.nodes}` 过滤 WorkNode，render 时再次过滤（防御式）；确保 `_render_sticky_restored_messages` 只拿 ChatNode。

### 未做 / 后续

- **E2E 集成测试**：真实"发一条 turn → auto-compact 触发 → agent 调 get_node_context → 下一轮 context 里看到 sticky → 再一轮没调、counter 衰减 → 到 0 drop"的端到端路径没写，只有 helper 层单测。需要带 scripted provider + 真数据库的集成夹具，与其他 integration 同构。
- **joint-compact sticky 保留方案**：如上所述暂时 reset；若实测发现用户依赖 joint-compact 前的粘滞引用，再改成 MAX-merge。
- **前端不暴露 sticky_restored**：当前 canvas 不展示粘滞状态；调试场景可以加一个"这轮哪些节点在 sticky"的 debug 悬浮层，但不紧急。




## 2026-04-22/23 — 综合集成测试 + 8 个 bug 一轮扫清

接 pack/compact/merge/auto_plan 几块功能都已经在画布上跑起来之后，开了一次全量集成测试。选用场景是"长对话技术选型"——分支、合并、压缩、打包、drill-down、MCP 搜索、auto_plan 多重 decompose 都要走一遍。第一回合跑下来暴露了 4 个后端 bug，追加验证又挖出 4 个更深的 bug。这一轮把 8 个全部修掉 + 补了一些前端体验 + 更新了 PATCH API。

测试日志和细节落在 `docs/reports/2026-04-22-bugfix-verification-auto_plan.md`（本地 gitignore），下面只总结修法。

### Bug #1 `f3f02c5`：judge halt 四条消息硬编码英文

`judge_formatter.py` 的 `format_judge_pre_prompt` / `format_judge_post_prompt` / `format_revise_budget_halt_prompt` / `format_ground_ratio_halt_prompt` 原来全写死英文。workspace language = zh-CN 时，用户在对话画布上看到"I seem to be spinning..."非常出戏。加了 `_is_zh()` 读 `tenancy_runtime.get_settings(DEFAULT_WORKSPACE_ID).language`，四个函数每段文本按 zh/en 分支生成。tests 路径无 `tenancy_runtime` 时 fallback en-US，不破坏 34 条相关单测。

### Bug #2 `bf59821`：手动 compact 漏写 `chat_compact` BoardItem

`compact_chain` 是唯一一个 ChatNode-producing 路径漏掉了 `_spawn_chat_board_item(compact_node)` 的调用——auto-compact 和 merge 两条都有。后果是：后续任何走这个 compact 的 compact / pack / merge 都会被 PR A 的 brief-sync gate 拒绝（"missing scope='chat' ChatBoardItem for ancestor..."）。在 compact 末尾补一行调用，与 merge 对齐。

### Bug #3 `4582ba6`：delegation-depth fuse

2026-04-22 集成测试里，"DeepWiki + Cloudflare docs + 综合"这么一句提示词让 planner 递归分解出 62 个节点、3 个 tool_call——手动取消前还在深入。加 `WorkFlow.delegation_depth` + `delegation_depth_budget`（默认 2）字段；`_build_sub_workflow_for_subtask` 传播深度 +1；`_after_planner_judge` 的 decompose 分支先判深度，超预算就强转 atomic（退化成 single-draft worker）。这样 L0 可以分解 → L1 可以分解 → L2 原子。一层多点 fan-out 足够大多数多角度任务，更深的递归几乎都是 over-decomposition。

### Bug #4 `3e13494`：dangling decompose 收尾恢复

`_run_node` 包了 post_node_hook 的 blanket try/except（合理——不 catch 会 cancel 整个 `asyncio.gather` 里的兄弟）。副作用：`_after_delegation` 里 `_spawn_judge_post` 若抛异常会被吞，整个 decompose group 没聚合判官，turn 以 "inner workflow had no terminal llm_call" 收场。加 `_execute_node` 的 post-execute 恢复扫描：如果看到有 plan_judge 拥有的 decompose group 全部成员都 terminal 但没 judge_post 覆盖，就补 spawn 一个并重跑 execute。非 race 路径零代价。

### Bug #5 `ff33413`：delegate brief 选错了 sub 终端

`_render_source_output_for_brief` 给 DELEGATE 挑 sub 的"效果产物"时，朴素地取最新非 BRIEF 节点的 output_message。auto_plan 子 WorkFlow 的时序尾巴是 judge_post，其 `content` 是空串（verdict 放结构化字段）。结果：brief LLM 接到空 content，写"该委托节点未产生任何输出内容"。给 7 个真实 delegate 做过回归：修前 7/7 全空，修后 2/7 直接是 worker 正文、4/7 是 plan JSON（decompose 未到 worker）、1/7 是"sub 只 failed 了几次 pre_judge"的 sentinel（这个判对了）。

新优先级：`judge_post.accept.merged_response` → 最新 `role=WORKER` DRAFT → 最新非 BRIEF / 非 JUDGE_CALL DRAFT → sentinel。顺便过滤空 content 和 judge_call content，避免空尾节点覆盖真实来源。

### Bug #6 `9749e6d`：retry-halt 消息 + 缺 failure reason

`judge_formatter` 的四条 halt message 在 #1 已经 i18n 了，但 `_compose_retry_halt_message` 是另外一条独立路径，继续吐英文。用户 zh-CN 下跑进 retry budget 熔断看到"I retried 3 round(s) but hit the retry budget..."。加 `_STATUS_HUMAN_ZH` 对照表 + `_retry_halt_is_zh()` helper，整段文本按语言出。

同时修复另一个隐蔽 bug：每个非 ok 成员原来只会写状态短语（"sub-agent refused before starting"），把 `_classify_sub_outcome` 其实已经建好的 `body`（具体 blockers / missing_inputs / worker error 原文）扔了。现在附上，截断 240 字，用户能一眼看清每条 subtask 真的是怎么挂的。

### Bug #7 `2ade0fd`：planner 偏向 atomic 的 prompt 重写

集成测试里即使是不复杂的多段回答（"总结+比较+推荐"三段），planner 也倾向拆成三个 sub_agent。加 `delegation_depth` fuse 只是护栏，不解决单层过度分解。重写 `plan.yaml`（zh + en）的决策部分：
- 显式说"**默认 atomic**，decompose 只在 gate 满足时才用"
- 3 条"OK to decompose"（真并行 / 子任务是完整任务 / 一个 worker 的 ReAct 装不下）
- 4 条"禁止 decompose"（N 段文字答案 / 强依赖的 next-step / 纯串工具调用 / 子任务≥5）
- 成本提醒："每个 decompose subtask 至少 5 次 LLM + 多个 brief"

效果：同一条 5 节分析 prompt 修前 124 节点 13 delegate 最终 failed，修后 18 节点 0 delegate 55s 完成（ReAct loop 里连续读 2 个文件出报告）；数据源真独立的三方对比仍然正确走 decompose。

### Bug #8 `ab2bc21`：decompose 聚合门槛放宽到"任一 terminal"

`_after_delegation` 和 `_recover_dangling_decompose` 原来都要求 `all(SUCCEEDED)` 才 spawn 聚合 judge_post。当某个 delegate FAILED，剩下的 delegate 产物就没机会被聚合——turn 直接以"no terminal llm_call"炸掉。改成 `all(status in {SUCCEEDED, FAILED, CANCELLED})` 即可聚合。aggregator judge_post 已经有 `_format_decompose_aggregation` 处理混合结果 + 结构化分类（#6 那条已经让 retry-halt 呈现 per-member reason），上层要 merge 还是 retry 还是 halt 都有判据。

### 扫除阶段 9-12：前端 + API 体验修补

- `9da804f` **MemoryBoardPanel**：顶部加了 6px 的拖动条（bottom-anchored 面板直观往上拖变长），加了最大化按钮（256px ↔ 70vh 一键切换）。拖动期间直接改 DOM `style.maxHeight`，mouseup 才 `setState`——60 fps × N 个 BoardItem 的 reconciliation 压力降到 0 re-render / 帧。
- `5c4970b` **PATCH /chatflows/{id}** 补 `tool_loop_budget` 和 `auto_mode_revise_budget`：两个字段都在 `ChatFlow.payload` 里但 PATCH 白名单忘了路由。深读代码类 prompt 经常触达默认 12 的 tool-loop 上限但改不了，只能直连 DB。
- `956091a` **Compact brief+id 降级**：一次自分析 turn 里 compact worker 崩在 `ProviderError: 500 from llama`。查下来 input = 105,571 tokens，compact_model（qwen36-27b-q4km）窗口 98,304——输入就溢出 compact 自己的窗口。加 preflight：serialized head > 90% compact_model window 时，把每个 ancestor message 改成 `[node:<id>] <MemoryBoard brief>`（brief 一行，没 brief 就截 400 字）再送去压缩。下游 worker 需要细节时走 `get_node_context` 回溯（Pack 同款 citation 路径）。
- `3f90ad1` **Planner scope 边界**：plan.yaml 加一段说明"`get_node_context` 只能拉 ChatFlow 层 ChatNode，不能拉兄弟 WorkNode；上游 sibling delegate 的输出已经被 engine 自动注入到 judge_pre 的 input_messages 顶部"。修之前 compare turn 的聚合 sub 经常误以为能用 `get_node_context(node_id=<兄弟 id>)` 拉上游，pre_judge 判 infeasible → halt。
- `89923c8` **Judge parse retry 从 1 次升到 2 次** + 指数退避 0.5s → 1.0s。ark-code-latest 偶发空 content / 非 JSON，单轮 retry 不够——典型 failure 现场是两次都空、第三次成功。代价只是失败场景多跑 1.5s + 两个 provider call，省下的是几十个上游 WorkNode 的工作。

### 覆盖数字

本轮共 13 个 commit push 到 `main`。所有修改点都经过单测：`343 passed` on backend unit（忽略预存损坏的两个文件），`72 passed` on frontend vitest。两个实盘 ChatFlow 保留在本地供复现：
- `019db728-606d-7652-9b47-d026bec4d20e` — 第一轮综合测试（28 节点，多 pack / compact / merge / MCP）
- `019db7b1-2b6f-7191-9689-745c9017f0ec` — 第二轮回归 + self-analysis 对照（6+ 支分叉）

### 坑与决策

- **"Bug 会一起挖出来"**：这一轮 8 个 bug 里 4 个都是"tier 1 fix 把 tier 2 fix 的触发条件暴露出来"——比如 #3 delegation-depth fuse 先压住失控递归，#4 dangling decompose recovery 才在 partial 路径上有机会生效；#6 surface reason 要等 #8 terminal 聚合放宽之后才有"实际能看到的失败原因"需要渲染。改顺序错了会误以为 fix 无效。
- **不要在 schema 改动时依赖 uvicorn `--reload`**：本轮至少 3 次 `WorkFlow` / `ChatFlow` schema 加字段导致 reload 卡死，都是硬 SIGKILL 重启解决。凡是涉及 schema 的改动，现在默认是"改完手动重启 uvicorn"。
- **compact_model 的选择会反噬**：把 compact 跑在小模型上是很合理的优化（省钱省算力），但会落入 #956091a 这个陷阱。正确的护栏是 preflight + 降级，不是"告诉用户必须用大模型"。
- **planner prompt 太听话也不好**：#7 之后 planner 几乎全部走 atomic，甚至在真的该 decompose 的多源对比 prompt 也有概率不拆——第二支 test 要明确说"分别去看三个平台"才触发。等以后看到 atomic-worker 在 ReAct loop 里反复绕圈的场景再反向收一下 prompt。

### 未做 / 后续

- **E2E 回归自动化**：本轮所有验证都是手跑 + 看 monitor。该写一个跑 greeting + 同一个 prompt 的脚本，对比当前 git HEAD 和一个 baseline tag 的 turn shape（tree size / tc count / del count）输出 diff。
- **brief+id 降级之后的语义对齐**：compact worker 现在在 overflow 时看到的是 brief 摘要而不是原文，summary 本身的保真度下降。`get_node_context` 回溯路径是标配逃生舱，但需要在 worker prompt 里强化"summary 不足以答的时候主动拉"。
- **retry-halt 消息想做得更贴**：现在改了 i18n + reason body，但 body 是原样拼接 — 可以在 #6 路径上加一层"按 status 归类后给出建议动作"（retry budget 耗尽建议简化 / 单 sub halt 建议补输入 / 多 sub 全 failed 建议改 prompt）。
- **深度换广度**：记在 `project_agentloom_depth_over_breadth.md`。当前 `delegation_depth_budget=2` 是深度护栏；还缺广度护栏（N > 3 时 planner 自动切成分组结构）。


## 2026-04-23 夜 — 节点拖动位置丢失的两层 bug

用户反馈："有时候在 WorkFlow 和 ChatFlow 上拖动节点摆位置，一刷新位置就回去了。"
第一反应以为是前端防抖没 flush，补了 pagehide / beforeunload 的紧急 flush（`9be1ec9`）——但用户说"等了挺久才刷新"，500ms 防抖肯定早就过了。换角度重查：应该是缓存层的问题。

### 第一层（前端防抖 flush）— `9be1ec9`

`handleNodeDragStop` 用 `setTimeout(flushPositions, 500)` 防抖批量保存 drag，在 <500ms 窗口内刷新会丢失 PATCH。ChatFlowCanvas + WorkFlowCanvas 都加了：
- `pagehide` / `beforeunload` / `visibilitychange → hidden` 监听
- emergency flush 用 `fetch({keepalive: true})`（`sendBeacon` 只支持 POST，我们的端点是 PATCH）
- 交互期间的 500ms 防抖保留，只在"页面要关闭/切走"时 sync flush

这个修对但不是根因——用户等了好几秒以上，防抖早就 fired 了。

### 第二层（后端 frozen-guard 缓存撒谎）— `b0df3b3`

真正的根因：`_strip_workflow_sticky`（frozen-guard 检查时用来 normalize 待比较 dump 的 helper）漏了 WorkNode 的 `position_x/y`——只排除了 `sticky_notes`。

后果链：
1. 拖动 → 500ms 后 PATCH 正常发出
2. `patch_workflow_positions` 取 runtime（内存中的 ChatFlow）→ mutate runtime 里 inner WorkNode 的 position_x/y
3. `repo.save(chat)` 跑 `_assert_frozen_chatflow_nodes_unchanged(prior, chat)`
4. `_strip_frozen_exempt` 在外层 ChatFlowNode 正确排除了 position_x/y，但 `_strip_workflow_sticky` 递归到 nested workflow 时**没排**——prior 的 inner position_x=None，new 的 position_x=555，不等，抛 `FrozenNodeError` → HTTP 500
5. 500 返回了但**步骤 2 已经 mutate 了 runtime**。runtime 是 GET 的权威（`get_chatflow` 优先读 runtime）—— GET 继续显示"拖动生效"，看起来完全正常
6. 直到 runtime 被清空（uvicorn `--reload` 触发 / 某个操作调了 `engine.detach` / 服务重启），GET 才会回退到 DB，DB 从没保存过 position_x/y → 节点"snap back"到 auto-layout

这个 bug 的阴险在于 runtime 的"乐观缓存"掩盖了保存失败，用户看不到 500，甚至觉得拖动本来是 work 的，直到某个触发 runtime 丢失的动作。今晚因为频繁编辑代码 uvicorn `--reload` 在跑，runtime 掉得勤，才让用户恰好观察到。

修法：`_strip_workflow_sticky` 递归 strip 掉每个嵌套 WorkNode 的 `position_x/y`，和 outer 层对齐。

### 坑与决策

- **先看表层行为往往会错**：前端防抖缺 flush 是真的问题（`9be1ec9` 该修），但不是用户报告的那个。用户描述的时间窗口不兼容，要重想。
- **runtime 的乐观缓存是双刃**：正常情况下它让并发写时 GET 能看到未持久化的中间状态（体验好），但在 save 失败时它也会骗 GET。long-term 考虑是让 save 失败时回滚 runtime，但目前 `_attached_chatflow` / runtime mutate 的耦合不够干净。
- **`_strip_*_exempt` 系列应该 single-source-of-truth**：两个 strip helper 维护两份字段列表容易漂移。未来可以合并成一个递归遍历带 `exempt_fields_by_level` 字典。

### 未做 / 后续

- **save 失败时回滚 runtime mutation**：当前 `patch_positions` 是"先 mutate runtime → 再 save"。正确做法应该是"先 dry-run save → 通过后才 commit runtime mutation"。现在的修补是让 save 不失败，但 pattern 本身脆弱。
- **其它 mutate 路径的 exempt 对齐**：merge / pack / compact 的 `_spawn_chat_board_item` 链路也是 mutate runtime；`sticky_notes` 已经对齐了，别的字段可能还有漏的。值得系统性审一遍。


## 2026-04-23 深夜 — Canvas edge 加方向箭头

ChatFlow 和 WorkFlow 画布的 edge 之前只有线没有箭头——纯树场景问题不大（父在上、子在下一眼能看出来），但我们这是带 fork / merge / pack-drop / delegate fan-out 的 DAG，实际观察问题：

- merge 节点附近两条 edge 视觉上难区分"入边"和"出边"
- pack ChatNode 从 parent 底部掉下来，看起来像从 pack 向上流回 parent
- WorkFlow 里 planner → delegates（fan-out）和 delegates → post_judge（fan-in）在拥挤时混作一团

改 `rfEdges.push` 给每条边加 `markerEnd: { type: MarkerType.ArrowClosed }`，颜色跟 stroke 同步（pack rose / merge purple / planned gray / succeeded dark）。ChatFlowCanvas 用 16×16，WorkFlowCanvas 用 14×14（inner DAG 紧凑，大箭头会压到 card header）。72 前端测试全绿，typecheck 干净。

### 坑与决策

- **颜色必须跟 stroke 一致**：React Flow 默认的 `MarkerType.ArrowClosed` 会用 currentColor，在 SVG marker 里会 fallback 到 black。得显式传 `markerEnd.color`，不然 rose 线会带黑箭头，极难看。
- **尺寸不能一刀切**：ChatFlow 的 ChatNode 卡片大（token 徽章 + execution mode + 压缩快照），16px 箭头刚好；WorkFlow 的 WorkNode 小，继续用 16px 会盖到目标节点 header，降到 14px。


## 2026-04-24 凌晨 — 启动时清理 running 孤儿节点

调查 CF `019db7b1` 里 ChatNode `019db7e6` 残留的 running 态 WorkNode 时发现一个结构性问题：**引擎的 scheduler state（active tasks、retry timers、rate-limit 等待）全在内存**，进程被杀或崩掉后，DB 里保留为 `running` / `retrying` / `waiting_for_rate_limit` 的节点就永远没人兜底——UI 一直显示"在跑"，frozen guard 也不触发（这三个状态非 frozen），但新请求也不会 resume 它们。

### 做法

在 `ChatFlowRepository` 加 `sweep_orphaned_running_nodes`，接在 lifespan 里 `backfill_missing_node_index` 之后跑。逻辑：

1. 扫全部 chatflows
2. 对每个 chatflow，递归走 ChatNode → `chat_node.workflow.nodes` → 递归 `worknode.sub_workflow`
3. 状态是 `{RUNNING, RETRYING, WAITING_FOR_RATE_LIMIT}` 的节点一律转 `FAILED`，`error` 留原值（如果有）否则写 `"orphaned: engine restarted mid-run"`，`finished_at` / `updated_at` 打当前时间
4. 有变动的 chatflow 才 dump JSONB 回 payload，最后统一 commit

lifespan 日志一句话汇总：`orphan_sweep: transitioned N stale ... node(s) to failed`。

### 细节

- **`WAITING_FOR_USER` 不扫**：这是 auto-mode 的合法 halt-pending-resume 状态，跨重启持久化是设计使然，跟 orphan 无关。
- **不走 `ChatFlowRepository.save`**：那个方法跑 frozen-guard，但 running 非 frozen，没必要付这个成本；直接 mutate payload 更干净。node_index 不记 status，不用 rebuild。
- **`error` 保留原值**：如果节点是在 try/except 里已经写过 error 再崩的，保留那条更有诊断价值，只在空时才写通用 orphan marker。
- **单进程 dev 前提**：在多 worker / 多进程部署下，启动期 sweep 可能误杀另一个 worker 的 in-flight 节点。docstring 里注明；生产级部署需要分布式协调（不在本阶段 scope 内）。
- **hygiene check**：`test_repo_hygiene.py` 强制 repo 函数必须引用 `workspace_id`，全局 sweep 本来不需要，但为了过检查在 per-CF DEBUG 日志里带上 `row.workspace_id`——顺便也方便排查哪个 workspace 出了状况。

### 测试

`tests/backend/unit/test_orphan_sweep.py` 7 条：terminal / planned / waiting_for_user 不动；running/retrying/waiting_for_rate_limit 三种分别转 FAILED；递归嵌套 sub_workflow；保留预先写的 error；多 ChatNode 计数。7/7 绿，整套 unit 350 条全绿（两条 `_citation_fallback_header` / `_build_merge_segments` 的 pre-existing 坏 import 跟本 change 无关，各自是遗留重构残留）。

### 未做 / 后续

- **sweep 时机**：目前只在启动时扫。更稳的做法是加个周期性 sweep（watchdog），在运行期捕获那些引擎仍在跑但协程泄露的节点；MVP 够用。
- **lost-engine-state recovery**：sweep 把 RUNNING 转 FAILED，干脆地承认损失；更雄心的设计是把它们转回 PLANNED 让下次 submit 接管——但那需要知道中间状态（哪些 tool call 跑完了、哪些没），现在不存。暂不追求。
- **pre-existing 坏 import 扫除**：unit 套件里 `test_citation_fallback.py` 和 `test_merge_context.py` 都引了不再存在的内部符号；撞上一次重构没跟上。下次动 chatflow_engine 时顺手修掉。


## 2026-04-24 凌晨 — planner anti-decompose 过头：从 ark-code-latest thinking 里看出来的

`019db7b1` CF 的 Agentloom/Dify/LangFlow 三方对比 turn，ChatNode `019db7e1` 的 plan 节点（ark-code-latest）留下了 LLM thinking：

> "拆三个：分析本地 Agentloom / 搜 Dify / 搜 LangFlow，再加综合 = 4 个 subtask。**规则说子数量 ≥5 不行，四个也可以？不对，再想想**。或者 atomic？atomic 的话 worker 可以自己 ReAct 循环…… **拆的话每个子任务都要走一遍流程，成本更高**……atomic 一个 worker 就能搞定……"

这是一个近乎完美的"**prompt 过头**"证据——ark 自己拟出了正确分解（3 个并行叶子 + 1 个聚合），但被我们自己的 prompt 劝退回 atomic。拆开看三处病灶：

1. **"第 3 条最关键"误导**（`plan.yaml` OK to decompose 段）。规则 1（独立并行）和规则 2（子产物完整）才是主要信号；"ReAct 装不下"是罕见边界。标了"#3 最关键"反而让模型把前两条当次要。

2. **子任务数量上限模糊**。原文"≥5 合并成 ≤3"读起来像 4 也该避免，ark 逐字复读了这个模糊带。正常 decompose 2–4 个才是常态。

3. **成本提醒是单边的**。细致罗列了 5 LLM 调用 vs 4 的差价，但没讲**并行带来的墙钟延迟收益** —— 3 个独立信息收集任务，decompose 能把 3×T 压到 1×T。ark 于是只算了成本账，没算收益账，落位 atomic。

### 修法

两套 fixture (zh-CN / en-US) 对称改 3 处：

- OK-to-decompose 段：去掉"第 3 条最关键"，在规则 1 下方加**典型正例**（对比/评估 ≥2 个独立对象 → N 并行叶子 + 1 聚合 → 墙钟逼近单对象耗时）
- Must-not-decompose 段：子任务数量上限改成 "≥5 合并成 ≤4；**2–4 是正常范围，不必强行并到更少**"
- **成本提醒**改名**成本 vs 收益**：保留 LLM 调用差价，但补一段墙钟延迟收益 + 决策原则"独立可并行 → 延迟收益通常胜出；强依赖串行 → 额外调度纯属浪费"

47 个 template/parser 相关单测全绿。

### 方法论收获

**LLM 的 thinking 是最好的 prompt debug 日志**。ark-code-latest 在 thinking 里几乎逐字复读了我们自己的规则字眼（"成本更高" / "≥5 不行"），沿着规则链条推理到 atomic——这比任何"模型倾向如此"的黑箱归因都清晰。下次改 prompt 前应该养成**先看至少一条典型 thinking、用模型的话反推 prompt 坑**的习惯。

### 未做 / 后续

- **live 回归**：fixture 改了但没在 live 上跑同样的三方对比 turn 验证。ark-code-latest 是否真的会切到 decompose，只有跑一次才知道。
- **成本公式可不可以给硬数字**：当前"延迟收益通常胜出"仍是定性判断。如果能写成"独立对象数 ≥2 且每个耗时 ≥20s → 选 decompose"这类硬规则，模型更难在推理里打折。不过这种阈值要先有真实数据校准，暂不强加。

### live 回归补记 (2026-04-24 凌晨)

在原 CF `019db7b1` 上、同一 user message、同一 ark-code-latest 上重跑（新 ChatNode `019dbe40-...`, 690s, succeeded）：

- planner 走 **decompose, 4 subtasks**（3 并行信息收集 + 1 聚合），顶层 4 个 delegate + 1 post_judge，对比原 `019db7e1` 的 atomic 0 delegate 30 节点纯链式
- plan 节点 thinking 里几乎逐字反向复读了我们的新措辞：
  > "独立分析后再聚合对比，**符合并行 decompose 的条件**，**可以降低墙钟延迟**，所以选择 decompose…子任务数量是 4 个，**在 ≤4 的范围**"

A/B 干净。三处 prompt 改动逐条命中（`≤4 的范围` ← 数量措辞；`降低墙钟延迟` ← 成本 vs 收益段；`符合并行 decompose 的条件` ← canonical win case）。`feedback_debug_prompt_via_thinking.md` 方法论闭环应验。


## 2026-04-24 上午 — frozen-guard 审计 + 3 件收尾

沿着前一天的 "position 拖动丢失" 坑，系统审了一轮所有可能在 frozen 节点上 mutate 的路径。主线都 OK（exempt 集合 + `_strip_workflow_sticky` 递归覆盖了已知用例），但挖出 **1 个 concrete bug**、**1 个 brittle pattern** 和 **0 个回归检测网**。三件依次收尾：

### 发现 1 → fix：嵌套 sub_workflow 内 WorkNode 拖动不持久化

`patch_workflow_positions` 只认 `chat_node.workflow.nodes`（一级），`PatchPositionsRequest` schema 根本没 `sub_path` 字段。前端 `WorkFlowCanvas.tsx` 里显式 `if (subPath.length > 0) return` 跳过写入——注释甚至写了 `subPath > 0 path today only reads, never writes`。用户进 delegate 的 sub_workflow 视图拖节点 → **前端内存变，从不 PATCH**，刷新全丢。

修法对称小改：
- `PatchPositionsRequest` 加 `sub_path: list[str] = []`
- 后端复用已有的 `_resolve_workflow(chat, chat_node_id, sub_path)` 解析目标 WorkFlow（跟 sticky-notes 走同一路径）
- 前端 `patchWorkflowPositions` 新增 `subPath` 参数，`WorkFlowCanvas.tsx` 的 `flushPositions` / `emergencyFlush` 把当前 `subPath` 传下去，去掉 subPath > 0 的早 return

### 发现 2 → 注释：`sticky_restored` 写在 SUCCEEDED 节点上

`_update_sticky_restored_for_node` 严格看是在 frozen 节点上写非 exempt 字段。今天安全的原因微妙：写入发生在节点**首次存库前**（`prior` 里根本没这个 node id → guard 的 per-frozen-node 循环跳过它）。未来任何人写"给已存过的 SUCCEEDED 节点重算 sticky"的批处理，guard 会静默 trip、PATCH 丢改。在 `ChatFlowNode.sticky_restored` 的 docstring 里加一段**Frozen-guard note**，讲清楚 "仅在首次 save 之前写" 的约束和两种未来破局方案（加 exempt 或走旁路 save）。

### 发现 3 → 回归测试：`test_frozen_guard_exempts.py`

meta-test 三条，gitignored（本地 unit 套件）：

1. `test_frozen_guard_exempts_all_ui_only_fields`：构造一个冻住的 CF（外 ChatNode SUCCEEDED + 一级 WorkNode SUCCEEDED + delegate SUCCEEDED + sub_workflow 内 WorkNode SUCCEEDED），把所有 UI-only 字段全 mutate 一遍（两级 `position_x/y`、两级 `sticky_notes`、`pending_queue` 不在这个测试里因为它不在 frozen 时常 mutate）。guard 必须全放行。
2. `test_frozen_guard_rejects_semantic_field_change`：改外 ChatNode 的 `description`，guard 必须 trip。
3. `test_frozen_guard_rejects_nested_semantic_field_change`：改嵌套 sub_workflow 里 WorkNode 的 `description`，guard 必须 trip。

正反对照。未来任何人加了新 UI-only 字段但忘了 exempt，(1) 会 fail；改 exempt helper 时不小心放行了语义字段，(2)/(3) 会 fail。

### 测试全量

353 backend unit（+3 新 meta-test），72 frontend unit，lint 清。唯 `ChatFlowCanvas.test.tsx` 有 2 条 unused-import 警告是预存遗留，跟本次无关。

### 未做

- **meta-test 覆盖率更高的版本**：现在的 meta-test 靠手工枚举 UI-only 字段。更硬核的做法是给 `NodeBase` 每个字段打一个 `is_ui_only: bool` 标记，guard 自动读取。不做，因为当前字段数少，手工维护比元数据基础设施便宜。
- **前端可视化提示**：如果 UI 想提示用户"这是 frozen 节点上的 UI 编辑、仅影响布局不改语义"，可以在拖动 frozen 节点时给个不同的光标/提示。不急。


## 2026-04-24 上午 — ChatBoard 摘要渲染到 compact 气泡

backend 的 `/inbound_context` endpoint 早已在 `summary_preamble` 里融合了 pre-compact 祖先的 CBI 描述文本（[ChatBoard | 被压缩节点逐条摘要]\n- [id] desc），但前端 `CompactMessageBubble` 一直只渲染 `snap.summary` + preserved tail —— CBI 那段对用户不可见。这次给它补上。

不想让前端解析后端的 marker 字符串（脆）。改成**后端结构化发出** + 前端直接消费：

### 后端

`InboundContextSegment` 加可选字段 `cbi_entries: list[CbiEntry] | None`。`CbiEntry = {node_id, description}`。`build_inbound_context_segments` 在拼 `summary_preamble` 时顺手把遍历出的 CBI 条目装进这个列表；无描述时字段保持 `None` 让前端直接跳过渲染。`messages[0].content` 里的 marker 文本保留不动（LLM-facing 契约），结构化字段纯粹是给 UI 用。

新测试：`test_chat_board_descriptions_embedded_in_preamble_only` 追加了 `cbi_entries` 形状断言（pre-cutoff 序、post-cutoff 祖先的描述不泄漏）；新增 `test_cbi_entries_none_when_no_descriptions_provided` 锁 None-skip 路径。11 条 unit + 4 条 integration 全绿。

### 前端

- `schema.ts` 加 `CbiEntry` 接口，`InboundContextSegment.cbi_entries: CbiEntry[] | null`
- `ConversationView` 在原有 `stickySegments` 旁边新建 `cbiByCompactNodeId: Map<NodeId, CbiEntry[]>`，从 `summary_preamble` 段按 `source_node_id` 索引
- `ChatMessageBubble` 加 `cbiBullets?: CbiEntry[]` prop 并透传到 `CompactMessageBubble`
- `CompactMessageBubble` 在 `summary` 下方、`preservedBlock` 之前加一段带顶部分割线的 CBI 列表：
  - 每条显示 `nodeId.slice(0, 8)` 单色下划线按钮 + 描述文本
  - 按钮点击走 `selectNode(e.node_id)`，跳转到对应 ChatNode
  - `ev.stopPropagation()` 防止触发外层的 `onSelect`
- i18n 新增 `conversation.cbi_label`（zh-CN / en-US）

### 测试与验证

- backend 354 unit + 4 integration；frontend 72 unit + `tsc --noEmit` 清（除 2 条无关的预存 unused-import 警告）
- live backend `/openapi.json` 已刷新，`InboundContextSegment.cbi_entries` + `CbiEntry` schema 正确暴露
- 手工 verify：live DB 里没有带 CBI 的 compact chain（测试用 CF 都没过压缩触发线），但 unit 覆盖了路径。下次 compact 落地的 chatflow 打开会看到新渲染。

### 未做 / 后续

- **全量 multi-segment 重写**：memory 里记过 "ConversationView 未来应该直接消费 endpoint 的五段结构，而不是 walk visiblePath 自己从节点 model 推"。本次只接了 CBI 这段增量，完整重写涉及 rewrite 整个 `visiblePath.map()` 成 `segments.map()`，是更大的前端 refactor，留给下次。
- **CBI 点击跳转后的滚动定位**：`selectNode` 只设了 selectedNodeId，没有滚动到目标气泡。常用路径够用（用户看得到选中高亮），但目标节点被压缩进 summary 时无节点可跳，体验略糙。可以后面加一条"跳到该 node 时如果在 summary 里就高亮 CBI bullet 回去"的回流。


## 2026-04-24 下午 — ChatFlow 画布 compact/pack 折叠/展开

用户提议的一个新交互：右键 compact/pack 节点 → "折叠此范围" → 范围内节点从画布消失，host 节点吸收它们变成一个视觉聚合；右键 host → "展开此范围" 还原。动机是深嵌套/大 range 的 pack 读 DAG 压力太大，手动折叠让用户按需整理视图。

### 路线选择：B（fold 即视角）

之前讨论过两个路线：(A) MemoryBoardPanel 加"视角过滤" + fold 独立共存；(B) 放弃视角过滤，fold 是唯一的视角控制手段。

选 B 的关键论证是用户问的："如果视角过滤 + fold 同时存在，想返回被折叠节点的视角，怎么做？" —— 两个机制的合成规则（auto-unfold on panel click、fold 把 selection 提到 host 等）太复杂。B 把 fold 当作 "visible = 有效" 的唯一开关：折 = 隐于画布 AND 隐于 MemoryBoardPanel；展 = 还原。一致的 mental model，零合成规则，顺便**让 `build_inbound_context_segments` 对 pack 的盲区不再要命**（前端不再依赖它做视角过滤）。

### 架构：fold projection

核心是 `computeFoldProjection(chatflow, foldedChatNodeIds)` —— 一个纯函数算 `{ hidden, hostByHidden, countByHost }`：

1. **pass 1：收集 raw range**  
   - pack 节点：`packed_range` 直接作为 range  
   - compact 节点：沿 primary-parent 链向上 walk，收集祖先到 merge 边界（不跨 merge，和 `_build_chat_context` 语义一致）

2. **pass 2：rawHidden = union of all ranges**

3. **pass 3：effective folds = ranges filter fold_host NOT IN rawHidden**  
   —— fold host 本身被别的 fold 盖住时，自己就不是有效 fold，因为它不可见，边不能连它

4. **pass 4：attribution**。剩下的 effective folds 按 `(-rangeSize, id)` 排序，最大 range 优先。每个 hidden 节点被分配给第一个声明它的 effective fold。大 range 优先的关键论证：

   ```
   packA.range=[A,B,C], packY.range=[B]（packY.parent=B 是 fork 子节点，不在 packA.range 内）
   ```
   两个都折时，B 同时在两家 range 里。用 smaller-range 优先会让 A→B 路由成 packA→packY、B→C 路由成 packY→packA，出现**有向环**。larger-range 优先则统一归 packA，packY 作为 packA 的外部子节点挂下来，清洁 DAG。

### Edge re-route 规则

三条：

- `src` 和 `dst` 都 hidden：删
- `src` hidden：`fold_host(src) → dst`
- `dst` hidden：`src → fold_host(dst)`

重路由后 `src === dst` 的自环边删。最后用 `Map<"${src}->${dst}", Edge>` 去重（多条原边折到同一对 host 只保留一条）。

重路由边的 `sourceHandle` / `targetHandle` 留空（用默认侧边 handle），因为原来的 pack 底部/顶部 handle 绑定在 pack 自己的卡片上，re-route 终点不一定承认那些 handle 名。

### 实现落位

| 位置 | 改动 |
|---|---|
| `chatflowStore.ts` | `foldedChatNodeIds: Set<NodeId>` + `foldChatNode` / `unfoldChatNode`；`loadChatFlow` / `setChatFlow` 清空（ephemeral，刷新丢）|
| `ChatFlowCanvas.tsx::buildGraph` | 新参数 `foldedChatNodeIds`，调用 `computeFoldProjection`，rfNodes 过 hidden，edges 走 re-route map |
| `ChatFlowCanvas.tsx::ChatBoardPanel` | filter items by `!hidden.has(source_node_id)`，fold 同步到面板 |
| `ChatFlowCanvas.tsx::NodeContextMenu` | 新 `foldState: "none" \| "fold" \| "unfold"` 驱动菜单项文案 |
| `ChatFlowNodeCard.tsx` | `data.isFoldHost` / `foldedCount` 驱动加宽 + 加粗左边框 + ring + 新增 "内含 N" 徽章 |
| i18n | `ctx_fold_range` / `ctx_unfold_range` / `fold_host_badge` / `fold_host_hint`（zh-CN / en-US）|

### 嵌套语义的单测覆盖

`ChatFlowCanvas.test.tsx` 里 `describe("buildGraph fold projection")` 块 5 条：

- 单 pack 折叠 —— range 隐藏、root 入边 re-route、host data 标注正确
- 单 compact 折叠 —— 祖先全隐、root 直接归入
- Fork 外部子节点 re-route 到 host（关键场景，见架构讨论）
- 嵌套 pack —— 外层吞掉内层，内层 host 沦为 hidden，路径 `m1 → packA`
- 隐藏源的 brief 不 emit，可见 host 的 brief 保留

加顺便修掉的两条 pre-existing lint warning（`test_citation_fallback` / `test_merge_context` 的 unused import 那两个是 backend，这次动的是前端 `ChatFlowCanvas.test.tsx` 里的 `useChatFlowStore` / `ChatFlowNode`）。

### 测试通过

- frontend: 77 unit（72 → 77，+5 new fold tests），tsc --noEmit 完全清
- backend: 354 unit 保持绿

### 未做 / 后续

- **持久化**：目前 `foldedChatNodeIds` 是 ephemeral 的 Set，刷新就丢。localStorage per-chatflow 是下一步的自然升级，但 MVP 先不做；用户要是抱怨再加。
- **点击 MemoryBoardPanel 里不可见节点自动展开**：路线 B 下面板已经不列隐藏节点了，所以这条合成规则不再需要。如果以后改成"hidden 节点用 muted 样式仍列出"再想。
- **merge 的折叠**：当前 fold 只挂在 pack / compact 上。merge 节点语义上也"吸收"了两个分支，但 fold 设计不涉及它；保持简单。
- **pack_snapshot 的 preserved_messages 里如果有 wire message，range 外的 edge 怎么算**：暂时不考虑 —— preserved_messages 不对应 ChatNode id，不影响 DAG 结构。
- **自动 fold on overview 模式**：以后若加"全局 overview 视图"按钮，可默认折叠所有 pack/compact 做快速总览。现在用户手动触发即可。


## 2026-04-24 傍晚 — Fold 重做：synthetic 节点取代 "吸收式"

早上 fold 跑起来后用户上手发现两个问题：
1. 对 compact 节点折叠后，**祖先链里的 pack 子节点视觉上连到了 compact 的右边** —— 完全反语义（pack 本该挂下面）
2. fold host 是 compact/pack 自己这个设计 conflate 了 "summary 语义节点" 和 "视觉聚合" 两件事，用户心智模型更贴近"**前面凭空生一个 fold 节点，compact/pack 自己保持独立**"

用户重新设计了目标几何，基本是把 fold 做成**一个合成的视图占位节点**（类似 `chatBrief`），放在 host 前面。Edge 方向通过四个 handle 区分内外位置：

- **left (fold-input)**：上游链入 fold
- **right (fold-output-right)**：fold → host（compact/pack）+ 来自 range 末尾节点的 fork 子节点（"在 fold 末端冒出"的兄弟）
- **top (fold-output-top)**：来自 range 中间节点的 fork 子节点（"从 fold 内部冒出"）
- **bottom (fold-output-bottom)**：来自 range 任意节点的 pack 子节点（保留 pack-below 约定）

### 实施

全仓只改 frontend：

- **新增** `frontend/src/canvas/nodes/ChatFoldNodeCard.tsx` + `ChatFoldNodeData`（最小形态：`折叠了 N 个节点` 标签，四个 Handle，虚线边框 + host 颜色渐染），注册 `chatFold` 到 `NODE_TYPES`
- **改 `computeFoldProjection`**：输出结构从 `{hidden, hostByHidden, countByHost}` 换成 `{hidden, foldByHidden, hostByFold, countByFold, lastMemberByFold}`。`foldByHidden` 映射到合成的 fold 节点 id（`_chat_fold_` 前缀 + hostId），不再直接指 host
- **`buildGraph` reroute 逻辑重写**：按 `(srcFold, dstFold)` 四种组合 + 内部再分"dst 是不是 host / 是不是 pack 子节点 / src 是不是 last member" 决定 `sourceHandle` / `targetHandle`。核心 insight：**edge 进 host 时用 `fold-output-right`，不论 host 是不是 pack**；只有 **pack 挂在 range 成员（非 host）下面** 才走 `fold-output-bottom`
- **删除** `ChatFlowNodeCard` 的吸收式变体（isFoldHost / foldedCount / "内含 N" 徽章），compact/pack 卡片保持原样
- **fold 节点右键 → 直接 `unfoldChatNode(hostId)`**，绕过 NodeContextMenu（fold 节点只有一种动作，弹完整菜单反而啰嗦）
- **onNodesChange / handleNodeDragStop**：fold 节点加入 synthetic id 过滤名单，不参与 position 持久化
- **i18n 换字串**：`fold_host_badge/hint` 废；`fold_node_label/hint` 加

### 几何约束 + DAG 投影为何仍需"防环"

用户有个尖锐的问题："原图本来就是 DAG 为什么还要防环？"—— 答案是 **quotient projection 会把 DAG 投出环来**，除非等价类保持凸性（convex subset）。当两个 fold range 有重叠但非嵌套、按小 range 优先 attribution 时，中间段节点被抠走，外层 range 剩下的部分不再连续，反向投影的 edge 就形成环。largest-range-first 的 attribution 配合 pack/compact range 的 primary-parent 连续性，**greedy 后每个类仍是 prefix / suffix 连续子链（凸）**，投影必然无环。这个推理写进了代码注释。

### Bug 回归

用户汇报的具体 bug："对某个压缩节点点了折叠，祖先链的 pack 显示连在压缩节点后面" —— 是旧版 "absorb" 设计下 re-route 把所有 `range_member → pack` 的边都终点换成 compact host，而 targetHandle 在 re-routed edge 下变成 `undefined`，React Flow 选了 compact 卡片的默认侧面（右边）作为 target。新设计里这类边明确走 `fold-output-bottom → main-target-top`，pack 稳定挂在 fold 下方。

### 测试

- 5 条 `buildGraph fold projection` 单测重写，新增覆盖 interior vs boundary fork handle、pack-below handle、nested pack swallow
- frontend unit: 78 绿（77 → 78，+1 新的 handle 测试）
- tsc --noEmit 清
- backend 不动，354 保持绿

### 未做 / 后续

- **持久化**：依旧 ephemeral
- **layout 碰撞**：合成 fold 节点不参与 `layoutDag`，直接在 host 左侧 `FOLD_WIDTH + FOLD_GAP` 处落位。若 host 的原位置太挤，fold 可能重叠到上游节点 —— MVP 先接受；若碰撞严重再把 fold 注入 dagre 当 "phantom chain node"
- **Fold 卡片 UI 升级候选**（记下）：
  - (b) 简短列出前几个被折节点的 id / brief
  - (c) 聚合统计：节点数 / 总 tokens / 消耗
  当前是 (a) 最小占位，以后用户喊不够再加
- **Merge fold**：当前 fold 仅挂 pack / compact，不涉及 merge


## 2026-04-24 夜深 — 持久化、pack 盲区、清 dead tests（overnight 推进三件）

用户睡觉前交代 overnight，做了三件：

### (1) Fold 拖动 + localStorage 持久化 (`b670c8e` + `97a6ced` + `e1087f9`)

- Fold 节点 `draggable: true`；位置走现有 `dragPositions.current`（session 内有效），`handleNodeDragStop` 分 brief / fold / real 三路（分别：无持久化 / 持久化到 store 但不 PATCH / 持久化到 store 且 PATCH）
- Store 新增 `foldPositions` 记录 per-host fold 位置，跟 `foldedChatNodeIds` 一起 write-through 到 `agentloom:fold:${cfId}` localStorage 键
- 顺手加 **view state 持久化**：新增 `agentloom:ui:last_chatflow`（全局）+ `agentloom:ui:view:${cfId}`（每 cf 的 selectedNodeId / drillStack / viewMode / workflowSelectedNodeId）
- `loadChatFlow` / `setChatFlow` hydrate 时必须 reconcile 对 live chatflow（过滤已删节点、截断断链 drillStack），否则会 "恢复到一个不存在的选中状态" 坏体验
- `deleteChatFlow` 同步 GC 对应 storage key，防 dead entries 积累
- **App boot 改写**：URL 无 `?chatflow=` 时 fall back 到 `last_chatflow`，plain 刷新能落回用户上次位置
- Store 级 `subscribe` 监听 view slice diff，signature 差才写，避免频繁 storage writes

Pattern 抽象写进 `feedback_localstorage_ui_pattern.md`：`agentloom:<scope>:<id>` 命名、3 helper 每类（load/save/clear）、try/catch 包 localStorage 异常、hydrate 必 reconcile、SSR guard。

### (2) `build_inbound_context_segments` pack 盲区修 (`b2fdac7`)

endpoint 之前对 chain 里的 pack ChatNode 无感：pack host 发 plain `ancestor`，range 成员也各发 ancestor，context preview 看到 pack 原文 + summary 双重曝光。现在 mirror `_build_chat_context` 的 `pack_subs_at` + `hidden_chain_indices`：

- Schema 新增 `pack_summary` kind（backend Literal + 前端 TS union），区分于 compact 顶部的 `summary_preamble`
- Segment builder 先计算 pack_subs_at（first-range-idx → pack node）和 hidden_chain_indices（range 成员 + pack 自身）
- 发射循环按 index 走，pack-sub 处 emit 一个 `pack_summary`（synthetic=True）+ 若有 preserved 再 emit 一个 `preserved`
- 新增 4 条单测：基本 pack-in-chain 折叠、preserved 分段、pack 在 compact 之上被 compact 吸收、pack 在 compact 之下正常 emit
- 362 backend unit + integration 绿，tsc 清

### (3) 删 pre-existing broken unit tests

`tests/backend/unit/test_citation_fallback.py` + `test_merge_context.py` 引用的 merge-rework "richer API" 从未 ship（简化版本里 dropped）。每次 pytest collection 都报 ImportError，`--ignore` 已经飞了好几周。两个文件 gitignored 所以直接删掉 = pytest collection 干净。`project_agentloom_merge_rework.md` 同步更新为 "richer API 正式 abandoned"。

### 方法论收获（沉淀到 memory）

- **`feedback_dag_projection_cycles.md`**：quotient projection of a DAG by non-convex classes can cycle。canvas fold 的 largest-range attribution 不是随便选的 —— 是 "range 为连续链 + greedy largest-first → 等价类保持连续子链 → 凸 → 投影无环" 的推理结果。未来做类似合并/折叠视图操作时回来看这条。
- **`feedback_localstorage_ui_pattern.md`**：per-chatflow 持久化的命名约定 + 3-helper 模板 + reconcile 强制 + GC 习惯。下次加新持久化域（toolbar 展开、画布 zoom 之类）照同一套写。


## 2026-04-25 上午 — 嵌套 fold：split attribution + containment edge

用户看 `019db244-afbe-7fe1-aa41-e848f85e624a` 那个 CF（一个 pack 在 compact2 的 range 里、pack 自己不在 compact2.range 里的 fork 场景）时发现：同折 compact2 + pack 之后，largest-first attribution 把 pack.range 的 3 个 hidden 节点全归给 compact2，pack 的 fold "没吃到 hidden 节点就 emit 了个空壳"，视觉上两个 fold 看起来像**孤儿**，读不出结构关系。

### 设计：strict-nested 检测 + split 归属

新 gate：当 B.range ⊆ A.range、B.host ∉ A.range、且 B 在 A.walk 里占据**连续前缀或后缀**（即 outer-exclusive 仍是凸的）时，**拆分归属** —— A 只吃 outer-exclusive、B 吃自己的 range。两个 fold 都留下 rfNode；A→B 的 crossing edge 是视觉上的 containment 链。

关键 cycle 安全性：只允许 inner 在 outer 的端点，**中间嵌套依旧走 largest-first 兜底**（比如 `packOuter.range=[a,b,c,d,e]` + `packInner=[c]`，split 会制造 outer→inner→outer 环；`innerOccupiesEndOfOuter` gate 检测到后 fall back）。推理等价于 `feedback_dag_projection_cycles.md` 的凸性保证。

### Orphan fold 过滤

顺带把 partial-overlap 下被 largest-first 吃空的小 fold 过滤掉：`claim.size === 0` 的 fold 不 emit rfNode，不再留悬空的 "folded N nodes" 空壳。

### 代码改动

- `computeFoldProjection`:
  - 新 helper `innerOccupiesEndOfOuter(walk, innerRange)` —— 凸性 gate
  - 新 helper `foldWalkOrder(chatflow, hostId, range)` —— pack 返 packed_range reverse，compact 返 primary-parent walk，统一轴向让两者可对比
  - 双 pass attribution：第一遍每个 fold baseline claim = range \ 直接 inner ranges；第二遍 largest-first 消 tie 和 partial overlap
  - 新输出 `nestedInnersByOuter: Map<foldId, Set<foldId>>`
  - Pass 5 emit hostByFold 时过 empty-claim 滤

- `buildGraph`:
  - `srcFold && dstFold` 分支新增 nested 检测：如果是 outer→inner 对，目标 handle 走 `fold-input`
  - containment edge 样式：`stroke = #94a3b8`（slate-400 muted）+ `strokeDasharray = "6 4"`（虚线）+ arrow color 跟着 stroke

### 测试（3 新 case）

- `strict nested fold: inner pack inside compact, both visible, split attribution + containment edge` —— 覆盖用户那个 CF 的核心 case：两 fold 都 emit、foldedCount 没重复计、containment edge 有正确 handle + 虚线 slate 样式
- `inner in middle of outer walk falls back to largest-first (no split)` —— 凸性 gate 生效
- `partial-overlap fold with zero claim is filtered (no orphan fold card)` —— orphan 过滤器生效

81 frontend unit + tsc --noEmit 清。

### 效果对比

原来（用户报告的 bug）：
```
fold_compact2(9 个, claim 全部)    fold_pack(3 个, claim 0)  ← 悬空 orphan
              ↓                                ?
          compact2                            pack
```

现在：
```
fold_compact2(6 个) ═══▶(虚线 containment)═══▶ fold_pack(3 个) ──▶ compact2
                                                            └──▶ pack
```

两个 fold 之间的嵌套关系靠虚线 slate 的 containment edge 一眼能读。


## 2026-04-25 下午 — fold 三件套补齐 + 注 dagre + token 统计

顺着用户的建议三连打：

### 1. Fold merge 节点

merge 语义上也 "吸收" 两条分支，挂 fold 和 pack/compact 同构。差别是 range 要走两条 primary-parent 链到 LCA（exclusive）:

- `findPrimaryLca(chatflow, leftId, rightId)` —— 前端版 LCA finder，walk 左链建 Set，从右链往上找首个命中
- `computeFoldProjection` 新分支：`node.parent_ids.length >= 2` 走 merge range 计算，union 左右两支 walk
- `lastMemberByFold` 从 `NodeId` 改成 `Set<NodeId>`：merge 有两个 "last member"（即 merge.parent_ids），边界 fork 检测用 `.has(parentId)`
- merge 折叠**不走嵌套 split**：`foldWalkOrder` 对 merge host 返空 walk，nested gate 自动失效；merge 永远走 largest-first。单链的凸性保证在多分支场景里不再适用，留它简单正确就好
- `ChatFoldNodeData.hostKind` 加 `"merge"` 变体，卡片紫色 tint 与 merge 节点原本的紫色 accent 呼应
- 上下文菜单 `isFoldableHost` 加 `parent_ids.length >= 2` 识别

新单测 `merge fold: range = both branches up to LCA, hostKind='merge', both parents are boundary members` 钉住 range 覆盖 + handle 路由。

### 2. Fold 注 dagre

原来 fold rfNode 手动放在 `host.x - FOLD_WIDTH - FOLD_GAP`，密集上游有机会叠到别的卡片上。改成**把 fold phantom 注入 `layoutDag` 输入**：

- `layoutInput`: 原 chatflow.nodes 去掉 hidden，加入 fold phantom；每个可见节点的 `parent_ids` 通过 `renderId` 重写（hidden parent → 对应 fold id）
- Fold phantom 的 `parent_ids` = "external upstream" = fold range 成员的 parents 里所有不在自身 range 内的（也通过 `renderId` 穿越 fold-of-fold 链）
- Phantom 的 `created_at` 取 range 里最早的一个，保持 stable sort 自然顺序
- 调 `layoutDag(layoutInput, [preservedRoots, ...extraRoots])`，phantom 位置从布局结果直接读
- Edge reroute 仍然迭代**原始** `chatflow.nodes`（要看 hidden→hidden 的 range 内部 edge 来做 containment 判定）

副作用：之前为 fold 手动算的 `FOLD_WIDTH` / `FOLD_GAP` 常量删掉。dragPositions 优先级不变，用户手动拖过的位置仍覆盖 dagre。

### 3. Fold 卡片 token 统计

卡片 body 除了 "折叠了 N 个节点"，再加一行 "{X} tokens"（用现成 `formatTokensKM`）。

- `ChatFoldNodeData.foldedTokens`: sum of `nodeTokens(node)` across 被该 fold attributed 的 hidden 节点
- `nodeTokens` = `entry_prompt_tokens + output_response_tokens`（跟 ChatNode 卡片 token bar 同源，legacy 节点的 WorkNode usage fallback 也走同条路径）
- 卡片上 `foldedTokens > 0` 才显示，predating 节点就隐掉
- i18n `chatflow.fold_node_tokens_label` + `fold_node_tokens_hint` 两条

新单测 `fold card foldedTokens aggregates entry_prompt + output_response across claimed range members`。

### 测试

83 frontend unit（82 → 83，+1）、tsc --noEmit 清。没 backend 动。


## 2026-04-25 傍晚 — Orphan sweep 周期 watchdog

startup sweep 只在进程启动时跑，能清前一轮崩溃留下的幽灵，但进程仍活着时 **scheduler 协程泄露**（async task 抛未处理异常静默退出、没把 RUNNING 节点转成 FAILED）依旧会让 UI 显示"永远在跑"直到用户手动重启。watchdog 就是给这个 case 的 safety net。

### 实现要点

- `ChatFlowEngine.active_chatflow_ids()`：返回当前有至少一个 non-done task 的 chatflow id 集合。watchdog 用来**跳过活跃的 chatflow**，避免把正在跑的 turn 误杀
- `sweep_orphaned_running_nodes(..., skip_chatflow_ids=None)`：增加可选参数。默认 None 走原行为（startup 场景，所有 RUNNING 都是陈旧的），传 set 则跳过集合内的 chatflow（watchdog 场景）
- `_orphan_watchdog(app, interval=15*60)`：lifespan 里 spawn 的 background task，loop（sleep → sweep → log）；每次迭代都 `getattr(app.state, "chatflow_engine", None)` 懒取 engine（lifespan 启动时 engine 可能还没被 `_get_engine` 惰性创建出来）。迭代内部捕获异常 + log，绝不让一次 transient DB 错误杀掉 watchdog 本身
- Shutdown 路径：lifespan 的 `finally` 里 cancel watchdog task，同 mcp_task 一样的 `await + except` 模式

### 为什么用 activity check 而不是 time-based

时间阈值（RUNNING 持续 > N 分钟判断为 orphan）逻辑上简单但不准 —— 长 auto_plan turn 合法地跑 30+ 分钟很常见，N 设高了等待久、设低了误杀。activity check 精确：只要 engine 还在管这个 chatflow 的 task，就绝不碰。

### 未加的

- 命中时的结构化通知（push 事件 → 前端 toast）—— 现在只落日志，用户不主动翻日志看不到
- 可配置 interval —— 硬编码 15 min，后续想做再加 workspace setting
- 整进 periodic / Cron 的 monitoring 管道 —— 单进程 dev 场景够用，多副本生产需要分布式 lock 再说

### 测试

359 backend unit（358 → 359，+1 signature test 钉 `skip_chatflow_ids` 参数存在）+ 前端不动。Live backend reload 干净，watchdog 在后台静默运行（只有清到东西才 log，空轮次不刷屏）。

### 测试全量

- backend: 354 unit + 4 integration = 358 + 新加 4 pack tests = 362，all green。移除 2 个 dead test 文件后 pytest collection 不再需要 `--ignore`
- frontend: 78 unit + tsc --noEmit 清
- 两个新 memory + 三个旧 memory 状态刷新（planner-scope live 验证 / pack_v2 deferrals 关闭 / merge rework richer API abandoned）
