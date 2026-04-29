# Live-backend smoke scripts

每个脚本驱动 **运行中的** Agentloom 后端走 HTTP API + 真模型，按 feature 拆。跟 `tests/backend/` 的 pytest 互补 —— pytest 用 stub provider + TestClient（毫秒级、确定性、覆盖逻辑分支），smoke 用真栈（捕真 LLM 行为 / SSE / DB FK / 中间件），单元测 mock 掉的 surface 只能这层验。

## 跑

前置：`make dev` 或 `uvicorn agentloom.main:app --reload --host 0.0.0.0 --port 8000`，然后默认 `AGENTLOOM_BACKEND=http://localhost:8000`、`AGENTLOOM_SMOKE_PROVIDER=volcengine`、`AGENTLOOM_SMOKE_MODEL=doubao-seed-2-0-pro-260215`（火山引擎免费档）。改 env 切其他 provider。

```bash
# 单个 feature
python scripts/smoke/01_single_turn.py

# 全部按顺序（包括 combo）
bash scripts/smoke/run_all.sh

# 只跑特定脚本（按文件名子串匹配）
bash scripts/smoke/run_all.sh 01 02 combo
```

每个脚本 self-contained：建 chatflow → 跑测 → 删干净。退出码 0 = 全 PASS，非 0 = 至少一个 check FAIL。

## 现有脚本

| 文件 | feature | 估时 |
|---|---|---|
| `01_single_turn.py` | 单线程对话最小路径（POST + GET 一致性）| ~30s |
| `02_fork_branch.py` | fork 从非 leaf 节点（fork 不拒绝原则）| ~2min |
| `03_compact_drill_down.py` | 手动 compact + drill-down（get_node_context 拉回原文）| ~3min |
| `04_auto_plan_recon.py` | auto_plan + cognitive ReAct DAG recon（recon 路径 + 递归 fuse 验证）| ~5min |
| `05_capability_request.py` | worker emit marker → escalation → planner respawn 全闭环 | ~5min |
| `06_cross_chatflow.py` | 跨 chatflow 读取（toggle off → 拒，toggle on → 通） | ~3min |
| `combo_full_pipeline.py` | 综合：多 turn + fork + compact + 切 auto_plan + drill-down + UX 检查 | ~8min |

`run_all.sh` 跑全套约 **20-30 分钟**（取决于 provider 速度）。

## 添加新 feature 的脚本

模板：
```python
from _common import smoke, backend_client, create_chatflow, submit_turn, get_chatflow, delete_chatflow

async def main():
    async with smoke("XX 你的 feature 名") as report:
        async with backend_client() as client:
            cf_id = await create_chatflow(client, title="smoke XX")
            try:
                # ... 触发 feature ...
                report.add("某项检查", condition, "可选 detail")
            finally:
                await delete_chatflow(client, cf_id)

if __name__ == "__main__":
    import asyncio; asyncio.run(main())
```

约定：
- 文件名 `NN_short_name.py`（NN 两位数 sortable）或 `combo_*.py`
- 每个 `report.add(label, ok, detail)` 是一个 check；末尾自动汇总
- 失败时 `sys.exit(1)`，runner 会捕到
- **必须**在 `finally` 里 `delete_chatflow` 防留垃圾

`_common.py` 的 helper 列表：
- `smoke(name)` — 上下文管理器，包整个脚本
- `backend_client()` — async httpx client
- `create_chatflow(client, title=, execution_mode=, cognitive_react_enabled=, extra_patch=)` — 创建 + 配 patch 一步到位
- `submit_turn(client, cf_id, text, parent_id=None)` — 同步等结果
- `get_chatflow(client, cf_id)` — 拿完整 JSON
- `delete_chatflow(client, cf_id)` — best-effort 清理
- `find_worknode(chatflow, chat_node_id=, role=, step_kind=)` — 找匹配的 WorkNode
- `all_worknodes(chatflow, chat_node_id=)` — 列所有 WorkNode

## 跟 pytest 的分工

| 维度 | pytest (`tests/backend/`) | smoke (这里) |
|---|---|---|
| Provider | stub / `_echo_provider()` | 真模型（火山免费 / 自配） |
| HTTP | TestClient in-process | 真 HTTP 走 uvicorn |
| DB | 测试 schema fixture | 主 dev DB（注意：会留在 dev DB 里，但每脚本自删） |
| SSE | 直接读 EventBus | 真 SSE 端点 |
| 速度 | 单测 12s / 集成 50s | 全套 ~20-30min |
| 用途 | 内层逻辑回归、CI 必跑 | release / pre-merge 验整栈 / 调试模型行为 |

两者不替代。pytest 验**逻辑**，smoke 验**整栈在用**。
