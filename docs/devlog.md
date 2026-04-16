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

## 故意没动的（next-step 候选清单）

- 半自动模式 UI（关键帧画布、locked/unlocked 操作）
- MCP runtime wiring（M7.5 backlog，lib 已有）
- Skill 模块 / 记忆模块（全新模块，未设计）
- redo_aggregation 路径仍 flat-format（Phase 3 surface 出来的次级 gap）
- 通知外部通道（webhook / desktop / 邮件）
- **Conversation compaction**（三层压缩：ChatFlow / WorkFlow / UI）— 报告见 `docs/research-conversation-compaction.md`，待讨论

