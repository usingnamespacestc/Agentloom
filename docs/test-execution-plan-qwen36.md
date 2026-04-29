# Agentloom 全栈测试执行方案（qwen36-27b 本地模型）

> 给"另一个全新 AI"的执行手册。你没看过这个项目的对话历史，不要假设任何上下文，**严格按本文档操作**。所有路径都是绝对路径或基于 `/home/usingnamespacestc/Agentloom`。
>
> 本文档约定的运行模型：**qwen36-27b-q4km**（本地 llama.cpp 跑），**JSON schema 模式**。所有 task 都用这个模型。
>
> **铁则**：
> 1. 不要删除任何运行后的 chatflow / 文件 / 测试输出
> 2. Task 1-3 你扮演**对系统机制不熟悉的普通用户**——指令必须像真实用户那样开放、模糊、不直接说出 Agentloom 内部术语（"compact" / "fork" / "auto_plan" / "judge_pre" 等）
> 3. 跑完每个 task 后写一份 `summary.md` 在对应目录里，记录关键观察 + 失败原因 + 数据点
> 4. **不在 Agentloom 仓库里 commit 任何东西**——所有产出都在 `runs/<folder>/` 下（gitignored）

---

## 0. 准备工作（执行任何 task 前都要做完）

### 0.1 确认环境

```bash
cd /home/usingnamespacestc/Agentloom
# 确认 git 在 main 分支最新
git status -sb
```

### 0.2 确认后端在跑

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/docs
```

应该返回 `200`。如果不是 200：

```bash
# 启动 docker 依赖（postgres + redis）
docker-compose -f /home/usingnamespacestc/Agentloom/docker-compose.yml up -d

# 启动后端
source ~/miniconda3/etc/profile.d/conda.sh
conda activate agentloom
cd /home/usingnamespacestc/Agentloom
uvicorn agentloom.main:app --reload --host 0.0.0.0 --port 8000 \
  > /tmp/agentloom-backend.log 2>&1 &
disown
sleep 5
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/docs   # 期望 200
```

### 0.3 确认 llama.cpp 在跑 qwen36-27b

后端已经配过一个 OpenAI-compat provider 指向 `http://localhost:8001/v1`，模型 id `qwen36-27b-q4km`，`json_mode=schema`。验证 llama-server 在跑：

```bash
curl -s http://localhost:8001/v1/models 2>&1 | head -c 300
```

应该看到 JSON 含 `"id": "qwen36-27b-q4km"`。如果没在跑：

```bash
# 在另一个 terminal 起 llama-server（路径根据实际部署调整）
# 用自编译的 llama.cpp master，模型权重 Q4_K_M
# 注意：context 给 32k-48k 比较合适；至少留 1GB GPU headroom，否则 decode 速度掉 15×
# 参考命令（按实际权重路径调整）：
~/llama.cpp/llama-server \
  --model /path/to/qwen36-27b-q4km.gguf \
  --ctx-size 32768 \
  --n-gpu-layers 99 \
  --port 8001 \
  --jinja \
  --grammar-mode strict
```

### 0.4 验证 Agentloom 后端能调到 qwen36 + 把 provider id 存到 env file

```bash
# 抓 provider id 并存到一个 env 文件，后续每个 shell `source` 一下就有变量
PROVIDER_ID=$(curl -s http://localhost:8000/api/providers | python3 -c "
import json, sys
for p in json.load(sys.stdin):
    if 'localhost:8001' in (p.get('base_url') or ''):
        print(p['id']); break
")
echo "export PROVIDER_ID=$PROVIDER_ID"  | tee /tmp/agentloom-test-env.sh
# 验证：
curl -s "http://localhost:8000/api/providers/$PROVIDER_ID" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for m in data.get('available_models', []):
    print('  model:', m['id'], '| json_mode:', m['json_mode'], '| ctx:', m['context_window'])
"
```

后面每个新 terminal 开始前先 `source /tmp/agentloom-test-env.sh`，省得重复抓 + 减少手填占位符出错的概率。下面凡是 `$PROVIDER_ID` 都假设你已经 source 过这个 env file（别再手填）。

### 0.4.1 不要手填 provider id 到 python 字符串里

下面 0.5 节有个 python heredoc 里写着 `PROVIDER_ID = "<填 provider id>"`。**那是 python 代码里的字符串字面量，不会读 shell 变量**。每次跑那段 heredoc 之前，把 `<填 provider id>` 替换成你 0.4 抓到的真实 id。或者改用 `os.environ['PROVIDER_ID']` 直接读 shell：

```python
import os
PROVIDER_ID = os.environ["PROVIDER_ID"]  # 替代上面的硬编码
```

（推荐改成读 env，省得来回手填。）

### 0.5 确认 qwen36 model 上的采样参数

本测试要求 qwen36-27b-q4km 用以下采样参数（写到 provider 的 model entry 里，每次 LLM call 都按这个发）：

| 参数 | 值 |
|---|---|
| temperature | 1 |
| top_p | 0.95 |
| top_k | 20 |
| repetition_penalty | 1 |

**先查当前值**：

```bash
PROVIDER_ID="<步骤 0.4 抓到的 provider id>"
curl -s "http://localhost:8000/api/providers/$PROVIDER_ID" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for m in data.get('available_models', []):
    if m['id'] == 'qwen36-27b-q4km':
        print('temperature:', m.get('temperature'))
        print('top_p:', m.get('top_p'))
        print('top_k:', m.get('top_k'))
        print('repetition_penalty:', m.get('repetition_penalty'))
        print('json_mode:', m.get('json_mode'))
        print('context_window:', m.get('context_window'))
"
```

**如果 4 个采样参数任一为 null** 或跟上表不符，**PATCH provider 的 available_models 全列表**（PATCH 这个字段是 replace，不是 merge——必须把所有 model entry 完整传回去）：

```bash
PROVIDER_ID="<步骤 0.4 抓到的 provider id>"

# 先把现有 available_models 拉下来 → 改 qwen36 那一项的 4 个采样参数 → PATCH 回去
python3 << 'EOF' | tee /tmp/qwen36-sampling.json
import json, urllib.request

PROVIDER_ID = "<填 provider id>"
TARGET_MODEL = "qwen36-27b-q4km"

with urllib.request.urlopen(
    f"http://localhost:8000/api/providers/{PROVIDER_ID}"
) as r:
    cfg = json.load(r)

models = cfg["available_models"]
for m in models:
    if m["id"] == TARGET_MODEL:
        m["temperature"] = 1
        m["top_p"] = 0.95
        m["top_k"] = 20
        m["repetition_penalty"] = 1
        # json_mode 必须是 "schema"（如果不是，也一起改）
        if m.get("json_mode") != "schema":
            m["json_mode"] = "schema"

req = urllib.request.Request(
    f"http://localhost:8000/api/providers/{PROVIDER_ID}",
    method="PATCH",
    headers={"Content-Type": "application/json"},
    data=json.dumps({"available_models": models}).encode(),
)
with urllib.request.urlopen(req) as r:
    print(r.read().decode())
EOF
```

**改完再查一次确认**（重复上面那段查询，4 个值应该都不再是 null）。

### 0.6 确认 qwen36 已知特性

参考已记录的观察（不需要查证，了解即可）：
- **40 tok/s decode @ 32k-48k context**：每个 LLM call 大概 30-90 秒，长上下文下更慢
- **长 prompt 下 qwen36 倾向假装"无工具权限"拒绝调用**：开 json_mode=schema 后好一些（强制结构化输出），但仍可能失败
- **q4km 量化**：精度一般，不要期待跟 GPT-4 / Claude 同水平的推理质量

### 0.7 确认对比项目本地路径（task 3 用）

Task 3 要让 Agentloom 自分析 + 跟其它 agent 工具横向对比。这几个工具的本地路径事先确认下：

```bash
for d in claude-code-source-code codex-cli-source gemini-cli openclaw; do
  if [ -d "/home/usingnamespacestc/$d" ]; then
    echo "✓ $d: /home/usingnamespacestc/$d"
  else
    echo "✗ $d: 不存在！task 3 跑之前先 clone 到这个路径"
  fi
done

# opencode 是只装了 binary，没有 source（这是正常状态）
ls -la /home/usingnamespacestc/.opencode/bin/opencode 2>&1
```

如果哪个项目缺失了，task 3 跑之前补：

| 项目 | 期望路径 | 备注 |
|---|---|---|
| codex（OpenAI） | `/home/usingnamespacestc/codex-cli-source` | github 上 OpenAI 的 codex-cli 仓库 |
| claude code（Anthropic）| `/home/usingnamespacestc/claude-code-source-code` | Anthropic 官方 CLI 源码 |
| openclaw | `/home/usingnamespacestc/openclaw` | claude code 的开源 fork（社区项目） |
| gemini cli（Google）| `/home/usingnamespacestc/gemini-cli` | Google 官方 CLI |
| opencode | 只有 binary `/home/usingnamespacestc/.opencode/bin/opencode` | 没源码，task 3 让 Agentloom 靠训练知识 + 必要时上网查 |

### 0.8 创建顶层运行目录（filesystem 输出）

```bash
mkdir -p /home/usingnamespacestc/Agentloom/runs/批量测试
mkdir -p /home/usingnamespacestc/Agentloom/runs/典型测试
mkdir -p /home/usingnamespacestc/Agentloom/runs/tau-bench
```

### 0.9 Agentloom 基本操作 cheat sheet（必读）

文档里说的"文件夹"有两个概念，**别混**：

- **filesystem 文件夹**（`runs/批量测试` 等）：放 log / json 输出，操作 = `mkdir`、`>` 重定向
- **Agentloom UI 文件夹**：sidebar 里把 chatflow 分组用的，DB 里的 `folders` 表，操作 = `POST/PATCH/GET /api/folders`

下面是 task 2/3/4 都会用到的几个核心 API 操作。把这些命令存好。

#### 0.9.1 创建 UI 文件夹

```bash
# 创建一个名为 "典型测试" 的顶层 UI 文件夹（不嵌套）。
# 返回 {"id": "...", "name": "典型测试"}；记下 id。
TYPICAL_FOLDER_ID=$(curl -s -X POST http://localhost:8000/api/folders \
  -H 'Content-Type: application/json' \
  -d '{"name": "典型测试"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
echo "典型测试 folder id: $TYPICAL_FOLDER_ID"
```

类似地为 `批量测试` 和 `tau-bench` 各创一个。这一步在每个 task 开始前做。

#### 0.9.2 创建 chatflow + 配模型 + 立刻归入文件夹

```bash
PROVIDER_ID="<步骤 0.4 抓到的 provider id>"
FOLDER_ID="$TYPICAL_FOLDER_ID"  # 或对应的 task 文件夹

# 1) 创建 chatflow
CF_ID=$(curl -s -X POST http://localhost:8000/api/chatflows \
  -H 'Content-Type: application/json' \
  -d "{\"title\": \"自分析对比 $(date +%Y-%m-%d)\"}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
echo "chatflow id: $CF_ID"

# 2) 配置 execution_mode + 4 类模型全部 pin 到 qwen36
curl -s -X PATCH "http://localhost:8000/api/chatflows/$CF_ID" \
  -H 'Content-Type: application/json' \
  -d "{
    \"default_execution_mode\": \"auto_plan\",
    \"draft_model\":            {\"provider_id\": \"$PROVIDER_ID\", \"model_id\": \"qwen36-27b-q4km\"},
    \"default_judge_model\":    {\"provider_id\": \"$PROVIDER_ID\", \"model_id\": \"qwen36-27b-q4km\"},
    \"default_tool_call_model\":{\"provider_id\": \"$PROVIDER_ID\", \"model_id\": \"qwen36-27b-q4km\"},
    \"brief_model\":            {\"provider_id\": \"$PROVIDER_ID\", \"model_id\": \"qwen36-27b-q4km\"}
  }" > /dev/null

# 3) 把这个 chatflow 移到目标 UI 文件夹
curl -s -X PATCH "http://localhost:8000/api/chatflows/$CF_ID/folder" \
  -H 'Content-Type: application/json' \
  -d "{\"folder_id\": \"$FOLDER_ID\"}" > /dev/null

# 验证：列文件夹下所有 chatflow 应该看到刚才创的
curl -s "http://localhost:8000/api/chatflows" \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
items = data.get('items', data) if isinstance(data, dict) else data
for c in items:
    if c.get('folder_id') == '$FOLDER_ID':
        print(c['id'][-12:], '|', c.get('title', '')[:50])
"
```

#### 0.9.3 提交一轮 user turn（同步等结果）

每个 turn 都是一次 POST。**`max-time` 一定要给够**——auto_plan + qwen36 单 turn 容易 5-15 分钟，给 1800 秒（30 min）兜底：

```bash
# 普通追加 turn（在当前 leaf 后挂）
curl -s --max-time 1800 -X POST \
  "http://localhost:8000/api/chatflows/$CF_ID/turns" \
  -H 'Content-Type: application/json' \
  -d '{"text": "你的用户消息..."}'

# 返回 JSON: {"node_id": "...", "status": "succeeded|failed", "agent_response": "..."}
```

#### 0.9.4 fork（在中段非 leaf 节点上 spawn 新分支）

```bash
# parent_id 给一个非 leaf 的 ChatNode id —— 系统不会拒绝，会自动 fork
curl -s --max-time 1800 -X POST \
  "http://localhost:8000/api/chatflows/$CF_ID/turns" \
  -H 'Content-Type: application/json' \
  -d "{\"text\": \"分支问题: ...\", \"parent_id\": \"<某中段 ChatNode 的 id>\"}"
```

#### 0.9.5 手动 compact（task 2 用得上）

```bash
# 把某个 ChatNode 之前的链浓缩成一个 compact summary
curl -s --max-time 1800 -X POST \
  "http://localhost:8000/api/chatflows/$CF_ID/nodes/$NODE_ID/compact" \
  -H 'Content-Type: application/json' \
  -d '{
    "preserve_recent_turns": 1,
    "compact_instruction": "总结前面的对话，保留关键事实和决定"
  }'
```

#### 0.9.6 查 chatflow / WorkFlow 全景

```bash
# 完整状态（含每个 ChatNode 的 inner WorkFlow）
curl -s "http://localhost:8000/api/chatflows/$CF_ID" | python3 -m json.tool
```

#### 0.9.7 怎么"扮演普通用户"

文档说要避免 Agentloom 内部术语。意思是：

✗ 不要在用户消息里写："请触发 fork / 跑 auto_plan / 调 get_node_context / compact 一下"
✓ 要写："换个方向探索 / 帮我想想另一个方案 / 我之前提到的 X 具体是什么 / 先帮我总结一下到这里聊了什么"

系统看到自然的用户语言会 **自动** 触发对应的 feature——不需要也不应该在 prompt 里点名。

---

## 任务 1: 批量测试 — 跑全部已有 e2e

**目标**：把仓库里现有的端到端测试套件全跑一遍，验证当前代码状态。

**输出目录**：`runs/批量测试/`（filesystem）+ Agentloom UI 文件夹 `批量测试`（smoke chatflow 归这里）

### 1.0 先创 UI 文件夹

```bash
BATCH_FOLDER_ID=$(curl -s -X POST http://localhost:8000/api/folders \
  -H 'Content-Type: application/json' \
  -d '{"name": "批量测试"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
echo "批量测试 folder id: $BATCH_FOLDER_ID"
```

### 1.1 跑后端 pytest（unit + integration）

```bash
cd /home/usingnamespacestc/Agentloom
source ~/miniconda3/etc/profile.d/conda.sh && conda activate agentloom

mkdir -p runs/批量测试/pytest
python -m pytest tests/backend/ -v --tb=short \
  > runs/批量测试/pytest/output.log 2>&1
echo "exit: $?" >> runs/批量测试/pytest/output.log

# 抓汇总行
tail -20 runs/批量测试/pytest/output.log > runs/批量测试/pytest/summary.txt
```

期望：~857 backend 测试 / 4 skipped / 0 failed。如果失败，把失败的测试名记到 summary。

### 1.2 跑前端 vitest

```bash
cd /home/usingnamespacestc/Agentloom/frontend
mkdir -p ../runs/批量测试/frontend
npx tsc --noEmit > ../runs/批量测试/frontend/tsc.log 2>&1
echo "tsc exit: $?" >> ../runs/批量测试/frontend/tsc.log
npx vitest run --reporter=verbose > ../runs/批量测试/frontend/vitest.log 2>&1
echo "vitest exit: $?" >> ../runs/批量测试/frontend/vitest.log
```

期望：~91 frontend tests / 0 failed。

### 1.3 跑 live-backend smoke 全套

```bash
cd /home/usingnamespacestc/Agentloom
source ~/miniconda3/etc/profile.d/conda.sh && conda activate agentloom

mkdir -p runs/批量测试/smoke
# **关键**：smoke 默认用 volcengine（doubao），但本任务要用 qwen36 本地模型，
# 而且要保留 chatflow 不删 + 把它们都归到 "批量测试" UI 文件夹。
export AGENTLOOM_SMOKE_PROVIDER="<填入步骤 0.4 抓到的 provider id>"
export AGENTLOOM_SMOKE_MODEL="qwen36-27b-q4km"
export AGENTLOOM_SMOKE_KEEP=1                     # 不要 auto-delete
export AGENTLOOM_SMOKE_FOLDER_ID="$BATCH_FOLDER_ID"  # 创建后立即移到这个 UI 文件夹

# 注意：qwen36 + json_mode 跑 7 个脚本预计 60-90 分钟（vs volcengine 的 22 分钟）
bash scripts/smoke/run_all.sh > runs/批量测试/smoke/run.log 2>&1
echo "exit: $?" >> runs/批量测试/smoke/run.log
```

期望：47 checks PASS。如果有 FAIL：
- 完整记录每个失败脚本的输出 grep 出来
- **不要** drop chatflow，让数据保留方便事后 drill-in
- 注意 qwen36 跑 auto_plan recon 可能因模型质量出现 capability_request 路径不触发等"模型质量观察"——这些不是 engine bug，smoke 脚本里都有标 `ℹ` 处理

### 1.4 写 summary.md

`runs/批量测试/summary.md`：

```markdown
# 批量测试结果（2026-MM-DD）

## 环境
- backend commit: <`git rev-parse HEAD`>
- model: qwen36-27b-q4km
- provider: <provider id>

## pytest 后端
- pass / fail / skipped 数
- 失败测试（如有）

## frontend
- tsc 是否干净
- vitest pass / fail

## smoke (47 checks across 7 scripts)
- 每个脚本 pass / total + 时长
- 模型质量观察（如某个 short-circuit 了 / 某个 model-quality FAIL 了）
- 整体结论
```

---

## 任务 2: 典型测试 — 长上下文 + 嵌套 feature 的 ChatFlow

**目标**：用 auto_plan 模式跑一个真实复杂的对话，自然触发多个 feature（fork / merge / compact / pack / drill-down 等）。

**filesystem 输出目录**：`runs/典型测试/longchat/`
**Agentloom UI 文件夹**：`典型测试`（task 2 + task 3 共用）

**重要**：你扮演一个**没用过 Agentloom 的普通用户**。不要在你的对话指令里写"compact"、"fork"、"auto_plan"、"judge_pre" 这种系统术语。要像真用户那样自然提问，让系统自己根据触发条件激活 feature。具体口吻参考 0.9.7。

### 2.0 先创 UI 文件夹（task 3 也用同一个）

```bash
TYPICAL_FOLDER_ID=$(curl -s -X POST http://localhost:8000/api/folders \
  -H 'Content-Type: application/json' \
  -d '{"name": "典型测试"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
echo "典型测试 folder id: $TYPICAL_FOLDER_ID"
```

### 2.1 创建 chatflow + 配模型 + 归入 UI 文件夹（参考 0.9.2）

```bash
cd /home/usingnamespacestc/Agentloom
mkdir -p runs/典型测试/longchat

PROVIDER_ID="<步骤 0.4 的 provider id>"
FOLDER_ID="$TYPICAL_FOLDER_ID"

CF_ID=$(curl -s -X POST http://localhost:8000/api/chatflows \
  -H 'Content-Type: application/json' \
  -d "{\"title\": \"典型测试 长对话 $(date +%Y-%m-%d)\"}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")

echo "chatflow id: $CF_ID" > runs/典型测试/longchat/meta.txt
echo "folder id: $FOLDER_ID" >> runs/典型测试/longchat/meta.txt

curl -s -X PATCH "http://localhost:8000/api/chatflows/$CF_ID" \
  -H 'Content-Type: application/json' \
  -d "{
    \"default_execution_mode\": \"auto_plan\",
    \"draft_model\":             {\"provider_id\": \"$PROVIDER_ID\", \"model_id\": \"qwen36-27b-q4km\"},
    \"default_judge_model\":     {\"provider_id\": \"$PROVIDER_ID\", \"model_id\": \"qwen36-27b-q4km\"},
    \"default_tool_call_model\": {\"provider_id\": \"$PROVIDER_ID\", \"model_id\": \"qwen36-27b-q4km\"},
    \"brief_model\":             {\"provider_id\": \"$PROVIDER_ID\", \"model_id\": \"qwen36-27b-q4km\"}
  }" > /dev/null

# 关键：移到 UI 文件夹里！
curl -s -X PATCH "http://localhost:8000/api/chatflows/$CF_ID/folder" \
  -H 'Content-Type: application/json' \
  -d "{\"folder_id\": \"$FOLDER_ID\"}" > /dev/null
```

### 2.2 跑对话场景

用 `POST /api/chatflows/$CF_ID/turns` 提交一系列 turn。**用普通用户语气**，不要点系统功能名。每个 turn 等 200 响应再发下一个（auto_plan 一 turn 在 qwen36 上可能 5-15 分钟，**不要超时**——`curl --max-time 1800` 给 30 分钟容忍）。

**话题建议**：选一个真实的、需要多步骤推理 + 持续对话的话题，比如：
- 准备一次跨城出差（要订机票 / 酒店 / 算预算 / 改行程 / 对比方案）
- 写一篇技术博客（构思 / 大纲 / 章节起草 / 修改 / 引用查证）
- 学习某个新技术（基础 / 进阶问题 / 写示例代码 / 调错）

要点：
- **总共至少 8-10 个 turn**，每 turn 都要在前面 turn 的基础上继续
- **中间要让对话上下文足够长**（让自动 compact 有机会触发）
- **某一时刻要让用户"对前面某个具体细节回头查证"**（自然触发 drill-down，例如："你之前提到的 X 具体是哪个数字"）
- **某一时刻要让用户"换个方向探索"**（如果 UI 可达可触发 fork；本任务用纯 HTTP 跑无 UI 但仍可手动 fork：`POST /turns` 带 `parent_id` 指向中段非 leaf 节点）

每个 turn 提交完后，把请求 + 响应都存到 `runs/典型测试/longchat/turn-NN.json`。

### 2.3 跑完后导出 chatflow 全景

```bash
curl -s "http://localhost:8000/api/chatflows/$CF_ID" \
  | python3 -m json.tool > runs/典型测试/longchat/chatflow.json

# 抓所有 WorkNode 的 step_kind / role / status / tool_name 简表
python3 << EOF > runs/典型测试/longchat/worknodes.tsv
import json
with open("runs/典型测试/longchat/chatflow.json") as f:
    cf = json.load(f)
print("chatnode_id\tworknode_id\tstep_kind\trole\tstatus\ttool_name")
for cn_id, cn in (cf.get("nodes") or {}).items():
    wf = cn.get("workflow") or {}
    for wn_id, wn in (wf.get("nodes") or {}).items():
        print(f"{cn_id[-8:]}\t{wn_id[-8:]}\t{wn.get('step_kind')}\t{wn.get('role')}\t{wn.get('status')}\t{wn.get('tool_name') or ''}")
EOF
```

### 2.4 写 summary.md

`runs/典型测试/longchat/summary.md` 至少要含：

```markdown
# 典型 ChatFlow 测试（2026-MM-DD）

## 场景
（简述你选的话题 + 用户视角的对话目标）

## chatflow 信息
- id: <CF_ID>
- 总 turn 数: N
- 总 ChatNode 数: M（包括 root + compact 等系统插入的）
- 总 WorkNode 数: K（看 worknodes.tsv）

## 触发了哪些 feature
对照下面这张表，每个 feature 标"是否触发 / 几次"：
- [ ] fork（用户从中段节点提交新 turn）
- [ ] auto compact（系统自动插入 compact ChatNode）
- [ ] manual compact（如果用户要求"总结一下"系统是否真触发）
- [ ] drill-down（出现 get_node_context tool_call）
- [ ] auto_plan 完整 pipeline（judge_pre → planner → planner_judge → worker → worker_judge → judge_post 全链路至少一个 turn）
- [ ] cognitive ReAct DAG recon（任一 turn 的 judge_pre 出 2 个 instance + 中间 tool_call）
- [ ] capability_request marker（worker 输出含 `<capability_request>...`）
- [ ] judge_post retry（任一 verdict.post_verdict=retry 后 spawn redo clone）

## 模型质量观察
- qwen36 在哪些场景表现尚可
- 哪些场景出问题（比如 long context decode 慢 / json_mode 仍输出残破 / tool_call 拒绝执行）
- 失败 turn 的 ChatNode 是 `failed` 还是 `succeeded` with halt-template

## 异常 / bug 候选
列出任何看起来像 engine bug 而非 model quality 的现象
```

---

## 任务 3: 典型测试 — Agentloom 自分析 + 横向对比

**目标**：让 Agentloom（auto_plan + qwen36）分析 Agentloom 自己的代码库，并跟 codex / claude code / openclaw / opencode / gemini cli 多维度对比。

**输出目录**：`runs/典型测试/self-analysis/`

**用户视角**：你假装是个"听说过 Agentloom 但没读过代码"的工程师，想让 Agentloom 自己介绍 + 跟其他工具比一下，决定值不值得用。

### 3.1 创建 chatflow

按 2.1 的步骤，但：
- title 改成 `典型测试 自分析对比 <日期>`
- filesystem 目录用 `runs/典型测试/self-analysis/`
- 用同一个 `$TYPICAL_FOLDER_ID`（task 2 创建的，sidebar 里 task 2 和 task 3 的 chatflow 应该并排显示）

### 3.2 跑对话

按下面顺序提交 turn。每条都用普通用户语气，不要泄漏系统术语。

**Turn 1**：让它先扫一遍仓库
> "我在 `/home/usingnamespacestc/Agentloom` 这个文件夹下载了一个项目叫 Agentloom，看 README 像是个 agent 框架。能不能帮我大致看看代码结构、主要做什么、用什么技术栈写的？我对它还不太熟。"

**Turn 2**：让它细看几个关键模块
> "上面提到的核心模块里，比如 ChatFlow / WorkFlow 引擎那部分，能展开讲讲它跟我用过的 LangChain 或 LangGraph 比起来有什么不同的设计思路吗？"

**Turn 3**：触发对比

> "我现在在评估几个工具。Agentloom 跟下面这几个比一下，从我能想到的维度（功能、定位、上手难度、生态、运行成本、稳定性、开发活跃度）。我把这几个工具的源码也都下载到本地了，路径如下：
>
> - **codex**（OpenAI 的）：`/home/usingnamespacestc/codex-cli-source`
> - **claude code**（Anthropic 出的 CLI）：`/home/usingnamespacestc/claude-code-source-code`
> - **openclaw**：`/home/usingnamespacestc/openclaw`
> - **gemini cli**（Google 的）：`/home/usingnamespacestc/gemini-cli`
> - **opencode**：本地只有 binary（`/home/usingnamespacestc/.opencode/bin/opencode`），源码没下载，只能靠你的 training 知识 + 必要时上网查
>
> 你可以读这些目录的 README / 主入口代码 / package.json 来了解它们的设计。**不需要把每个工具的代码全读完**，扫一遍 README + 一两个核心文件就够了。
>
> 不需要面面俱到，先讲 3-5 个你认为最有区分度的维度，每个工具一两句话说清楚。"

**Turn 4**：让它深入某个对比维度
> "你刚刚说的那几个维度里，'X' 这点我还没完全理解。能用具体一两个使用场景举例子吗？"
（X 选 Turn 3 答里你最不确定的那个）

**Turn 5**：让它总结建议
> "OK，那基于上面的对比，如果我是一个 a) 主要写 Python 后端、b) 偶尔做 agent 实验、c) 不想自己搭服务的开发者，你最推荐哪个？为什么？也说说什么场景下应该选 Agentloom。"

**Turn 6**（drill-down 触发）：
> "你之前说的 Agentloom 那段关于 DAG 的描述，里面提到的 '某个具体术语 / 字段 / 文件名'（你回查 Turn 1-2 的回答里挑一个具体的细节）能再细说一下吗？"

每个 turn 的请求 + 响应同 2.2 存到 `runs/典型测试/self-analysis/turn-NN.json`。

### 3.3 跑完后做 3.3 同 2.3 的 chatflow + worknodes 导出

### 3.4 写 summary.md

跟 2.4 一样的 feature 触发表，加上对比表格：

```markdown
# Agentloom 自分析 + 横向对比（2026-MM-DD）

## 对比维度（Turn 3 答里 Agentloom 给出的）
| 工具 | 维度 1 | 维度 2 | 维度 3 | ... |
|---|---|---|---|---|
| Agentloom | ... | ... | ... | |
| codex | ... | ... | ... | |
| claude code | ... | ... | ... | |
| openclaw | ... | ... | ... | |
| opencode | ... | ... | ... | |
| gemini cli | ... | ... | ... | |

（直接抄 Agentloom 的回答，不要你自己补充）

## qwen36 + auto_plan 的真实表现
- 准确性观察（明显错的事实 / 幻觉 / 自相矛盾）
- 一致性观察（多 turn 之间观点是否一致）
- 时延（每 turn 平均多少秒）

## feature 触发表
（同 2.4 模板）

## bug / 异常候选
```

---

## 任务 4: tau-bench 大批量

**目标**：跑 τ-bench retail + airline 各**至少 15 个任务**，分别用 native_react 和 auto_plan 模式。

**filesystem 输出目录**：`runs/tau-bench/`
**Agentloom UI 文件夹**：`tau-bench`（每批跑完后用脚本把对应 chatflow 全部移进去）

### 4.0 先创 UI 文件夹

```bash
TAU_FOLDER_ID=$(curl -s -X POST http://localhost:8000/api/folders \
  -H 'Content-Type: application/json' \
  -d '{"name": "tau-bench"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
echo "tau-bench folder id: $TAU_FOLDER_ID"
```

### 4.1 准备

确认 agentloom-bench CLI 装着：

```bash
source ~/miniconda3/etc/profile.d/conda.sh && conda activate agentloom-bench
which agentloom-bench   # 应该有
```

如果没有，参考 `agentloom_bench/README.md`（如果在）或 `pyproject.toml` 装：

```bash
cd /home/usingnamespacestc/Agentloom/agentloom_bench
pip install -e .
```

### 4.2 retail / native_react

```bash
cd /home/usingnamespacestc/Agentloom
mkdir -p runs/tau-bench/retail-native-react
PROVIDER_ID="<步骤 0.4 的 provider id>"

# 跑 task 0-19（20 个）。注意 qwen36 跑一个任务 ~10-30 分钟，
# 整批可能 5-10 小时。建议放后台 + nohup。
nohup agentloom-bench \
  --domain retail --task-ids 0-19 \
  --backend-url http://localhost:8000 \
  --agent-provider "$PROVIDER_ID" \
  --agent-model qwen36-27b-q4km \
  --user-provider volcengine \
  --user-model doubao-seed-2-0-pro-260215 \
  --execution-mode native_react \
  --max-turns 30 \
  --out runs/tau-bench/retail-native-react \
  > runs/tau-bench/retail-native-react/run.log 2>&1 &
disown
echo "pid: $!"
```

### 4.3 retail / auto_plan

跑完 4.2 之后（不要并发，避免抢 GPU）：

```bash
mkdir -p runs/tau-bench/retail-auto-plan
nohup agentloom-bench \
  --domain retail --task-ids 0-19 \
  --backend-url http://localhost:8000 \
  --agent-provider "$PROVIDER_ID" \
  --agent-model qwen36-27b-q4km \
  --user-provider volcengine \
  --user-model doubao-seed-2-0-pro-260215 \
  --execution-mode auto_plan \
  --max-turns 30 \
  --out runs/tau-bench/retail-auto-plan \
  > runs/tau-bench/retail-auto-plan/run.log 2>&1 &
disown
echo "pid: $!"
```

### 4.4 airline / native_react + auto_plan

airline 只有 50 个任务，跑 0-14（15 个）。两种模式各跑一遍：

```bash
mkdir -p runs/tau-bench/airline-native-react runs/tau-bench/airline-auto-plan

# native_react
nohup agentloom-bench \
  --domain airline --task-ids 0-14 \
  --backend-url http://localhost:8000 \
  --agent-provider "$PROVIDER_ID" --agent-model qwen36-27b-q4km \
  --user-provider volcengine --user-model doubao-seed-2-0-pro-260215 \
  --execution-mode native_react --max-turns 30 \
  --out runs/tau-bench/airline-native-react \
  > runs/tau-bench/airline-native-react/run.log 2>&1 &
disown

# auto_plan（前一个跑完后再起）
nohup agentloom-bench \
  --domain airline --task-ids 0-14 \
  --backend-url http://localhost:8000 \
  --agent-provider "$PROVIDER_ID" --agent-model qwen36-27b-q4km \
  --user-provider volcengine --user-model doubao-seed-2-0-pro-260215 \
  --execution-mode auto_plan --max-turns 30 \
  --out runs/tau-bench/airline-auto-plan \
  > runs/tau-bench/airline-auto-plan/run.log 2>&1 &
disown
```

### 4.5 把 chatflow 移到 UI 文件夹

`agentloom-bench` 创建的 chatflow 默认放 workspace root（不归任何文件夹）。每个 batch 跑完后，把那个目录的所有 task_*.json 里的 `chatflow_id` 都移到 tau-bench UI 文件夹：

```bash
for d in retail-native-react retail-auto-plan airline-native-react airline-auto-plan; do
  echo "moving chatflows from $d ..."
  for f in /home/usingnamespacestc/Agentloom/runs/tau-bench/$d/task_*.json; do
    [ -f "$f" ] || continue
    cf_id=$(python3 -c "import json; print(json.load(open('$f')).get('chatflow_id', ''))")
    if [ -n "$cf_id" ]; then
      curl -s -X PATCH "http://localhost:8000/api/chatflows/$cf_id/folder" \
        -H 'Content-Type: application/json' \
        -d "{\"folder_id\": \"$TAU_FOLDER_ID\"}" > /dev/null
    fi
  done
done
```

每个 batch 完成后跑一次（或者四个 batch 都完成最后跑一次也可）。

### 4.6 监控 + 收尾

每个 batch 跑完后 `runs/tau-bench/<domain-mode>/` 会有：
- `task_<i>.json` × N（每任务一份）
- `batch_report.md`（自动生成的汇总）
- `run.log`（CLI stdout）

每个目录都有了再写 `runs/tau-bench/summary.md`：

```markdown
# tau-bench 大批量结果（qwen36-27b-q4km）

## 配置
- agent: qwen36-27b-q4km @ localhost llama.cpp（json_mode=schema）
- user simulator: volcengine doubao-seed-2-0-pro-260215
- max-turns: 30 / task

## 结果汇总

| domain | mode | tasks | reward=1 | reward=0 | error | avg duration | total time |
|---|---|---|---|---|---|---|---|
| retail | native_react | 20 | ? | ? | ? | ? | ? |
| retail | auto_plan | 20 | ? | ? | ? | ? | ? |
| airline | native_react | 15 | ? | ? | ? | ? | ? |
| airline | auto_plan | 15 | ? | ? | ? | ? | ? |

（每行的数字从对应目录的 task_*.json + batch_report.md 抓）

## 模式对比观察
- native_react vs auto_plan 在 reward 上差多少？
- auto_plan 多花的时间值不值得？
- qwen36 在 auto_plan 的 cognitive 角色（judge / planner）上表现如何？

## 失败案例 drill-in（任选 2-3 个 reward=0 的，深入 1 层）
- task X: 失败原因 = 模型答错 / 工具用错 / 系统 halt（看 transcript + chatflow 节点）
```

---

---

## 常见 footgun + 操作模板（出问题前看一眼）

### F1. 前端跑测试前要先 npm install

`npx tsc` / `npx vitest` 第一次跑要 `node_modules`：

```bash
cd /home/usingnamespacestc/Agentloom/frontend
[ -d node_modules ] || npm install
```

省得 1.2 节卡在依赖。

### F2. nohup 起的 batch 怎么"等"它跑完再起下一个

Task 4 的 4 个 batch 必须**串行**（共享 GPU）。`nohup ... &` 立即返回 pid，不会自动等。等的标准操作：

```bash
# 起 batch 后记下 pid
nohup agentloom-bench ... > runs/.../run.log 2>&1 &
BATCH_PID=$!
disown $BATCH_PID
echo "batch pid: $BATCH_PID"

# 等它结束（每 60s 检查一次；过程中不要起其它 GPU 任务）
while kill -0 $BATCH_PID 2>/dev/null; do
  sleep 60
  echo "[$(date +%H:%M)] still running, last log line:"
  tail -1 runs/.../run.log
done
echo "batch finished"
```

### F3. 怎么找"中段非 leaf ChatNode" 给 fork 用

Task 2 / task 3 / combo 都需要 fork。从 chatflow JSON 里挑一个有 child 的 ChatNode：

```bash
CF_ID="<chatflow id>"
curl -s "http://localhost:8000/api/chatflows/$CF_ID" | python3 << 'EOF'
import json, sys
cf = json.load(sys.stdin)
nodes = cf.get("nodes") or {}
# 谁是别人的 parent
parent_ids = set()
for n in nodes.values():
    for pid in n.get("parent_ids") or []:
        parent_ids.add(pid)
# 既是某 user-turn ChatNode 又是别人的 parent → 中段非 leaf
for nid, n in nodes.items():
    user_text = (n.get("user_message") or {}).get("text") or ""
    if user_text and nid in parent_ids:
        print(f"{nid}\t{user_text[:60]}")
EOF
```

挑任一行的第一列作 `parent_id` 给 0.9.4 的 fork POST。

### F4. POST /turns 拿到 status=failed 时怎么办

任一 turn 的响应可能是：

```json
{"node_id": "...", "status": "failed", "agent_response": "..."}
```

或者 `status="succeeded"` 但 `agent_response` 是 halt-template 文案（"无法处理 ..." 之类）。

**处理原则**：
- **不要重试**——失败本身是测试数据，要保留
- 把响应原样存 `turn-NN.json`
- summary 里标 "turn N 失败 / halted"，写明 status / agent_response 摘要
- **继续下一 turn**——后续 turn 可能在新分支或同分支恢复正常

如果连续 3 turn 都 failed，停下来检查（可能是 backend 挂了 / llama-server OOM / 上下文爆了）。

### F5. turn-NN.json 推荐格式

每个 turn 提交完，存这样一个 JSON 方便后续 drill-in：

```json
{
  "turn_index": 1,
  "submitted_at": "2026-MM-DDTHH:MM:SSZ",
  "duration_seconds": 384.2,
  "user_text": "...",
  "parent_id": null,
  "response": {
    "node_id": "019dd...",
    "status": "succeeded",
    "agent_response": "..."
  }
}
```

### F6. 验证 chatflow 真的用 qwen36（防默认模型回退）

创建 + PATCH 后查一下确认：

```bash
curl -s "http://localhost:8000/api/chatflows/$CF_ID" | python3 -c "
import json, sys
cf = json.load(sys.stdin)
for k in ('draft_model', 'default_judge_model', 'default_tool_call_model', 'brief_model'):
    v = cf.get(k) or {}
    print(f'{k}: provider={v.get(\"provider_id\", \"\")[-12:]} model={v.get(\"model_id\")}')
"
```

4 个 model 都应该是 qwen36-27b-q4km。如果有任一是空 / 别的模型，PATCH 不到位，turn 会 fallback 到默认（可能不是你想要的）。

### F7. UI 文件夹重名

POST `/api/folders` 不会拒绝同名（每次新建会拿到不同 id）。**每个 task 开始前**先看有没有同名残留：

```bash
curl -s http://localhost:8000/api/folders | python3 -c "
import json, sys
for f in json.load(sys.stdin):
    print(f['id'], '|', f.get('name'))
"
```

如果"批量测试" / "典型测试" / "tau-bench" 已经存在，**直接复用现有 id**（不要再创一个），把 chatflow 移进去就行——名字一样的两个 folder 在 UI 里看不出区别，乱。

### F8. 不知道就说不知道（task 3 特别注意）

opencode 没本地源码，task 3 的 Agentloom + qwen36 也不一定有 opencode 的 training 知识。Agentloom 内置工具里**没有 WebFetch / WebSearch**——这意味着 agent 实际上"上网查"做不到。

如果让 agent 比较 opencode，agent 应该：
- 能从 training 知识答出来 → 答
- 不知道 → **直接说"不熟悉这个工具"**，不要瞎编 feature

执行 AI（你）也注意：跑完 task 3 看 transcript 时，如果 Agentloom 给出明显幻觉的描述（比如说 opencode "支持 X 功能" 但训练数据里大概率没这个事实），在 summary 里**标为幻觉**——这是 q4km 量化模型的真实表现观察。

### F9. reward 数字怎么从 task_*.json 抓（task 4 summary 用）

```bash
for d in retail-native-react retail-auto-plan airline-native-react airline-auto-plan; do
  out="/home/usingnamespacestc/Agentloom/runs/tau-bench/$d"
  [ -d "$out" ] || continue
  python3 << EOF
import json, glob, statistics
files = sorted(glob.glob("$out/task_*.json"))
rewards = []
durations = []
for f in files:
    with open(f) as fh: t = json.load(fh)
    rewards.append(t.get("reward") or 0)
    durations.append(t.get("total_duration_seconds") or 0)
ones = sum(1 for r in rewards if r >= 1.0)
zeros = sum(1 for r in rewards if r == 0)
print(f"{$out!r:<60} N={len(files)} reward1={ones} reward0={zeros} avg_dur={(sum(durations)/max(len(durations),1)):.0f}s")
EOF
done
```

把每个目录跑出来的一行直接抄进 summary 表格。

### F10. 磁盘 / DB 检查

跑前看一眼：

```bash
df -h /home/usingnamespacestc | tail -1
# postgres 占用
docker exec $(docker-compose -f /home/usingnamespacestc/Agentloom/docker-compose.yml ps -q postgres 2>/dev/null) psql -U agentloom -d agentloom -c "SELECT pg_size_pretty(pg_database_size('agentloom'));" 2>/dev/null
```

如果磁盘 < 5GB / DB > 5GB，task 4 跑完很可能爆。提前清理或换盘。

---

## 出问题怎么办：debug 接力 protocol（必读）

测了这么多东西，**几乎一定会有问题**。这一节是给执行 AI（你）的"我跑完发现 X，怎么把 X 交接给下一个会 debug 的 AI" 操作手册。

### D1. detection — 跑测试时盯什么

实时跑的时候 + 每个 task 结束写 summary 时，主动找下面这些信号：

| 信号 | 在哪看 |
|---|---|
| pytest FAILED / ERROR | `runs/批量测试/pytest/output.log` grep `FAILED\|ERROR` |
| smoke ✗ check | `runs/批量测试/smoke/run.log` grep `✗\|FAIL\|exception` |
| Traceback / 5xx / unhandled exception | 所有 `*.log` grep `Traceback\|HTTPStatusError\|500 Internal` |
| ChatNode `status=failed` | 每个 chatflow JSON 里看 |
| WorkFlowNode `status=failed` 或 `error != None` | 同上 |
| Agent 回复含 engine-speak | grep `internal error\|system error\|系统错误\|内部错误` 在 transcript |
| auto_plan 没走完 pipeline（比如缺 worker / 缺 judge_post）| WorkFlow 简表（worknodes.tsv）看 role 分布 |
| recon DAG 跑超过 1 轮（fuse 失效）| 看 judge_pre 节点 count，>2 就异常 |
| backend 日志异常 | `tail /tmp/agentloom-backend.log` grep `WARNING\|ERROR\|Exception` |
| GPU OOM / llama-server 崩 | nvidia-smi memory + llama-server 进程是否还在 |
| tau-bench 里 `stop_reason=backend_error` 或 `agent_status_failed` | task_*.json |

### D2. 分类决策树

每发现一个异常，按这个树分类：

```
异常 →
├─ pytest / smoke / TS 编译失败？
│   └─ engine bug 候选（很可能是回归）
├─ tau-bench 里 reward=0 / status=failed？
│   ├─ 看 transcript：agent 答非所问 / 编造工具结果？
│   │   → model quality（qwen36 弱）
│   ├─ 看 chatflow：工作流 halt 在某 cognitive 节点？
│   │   → engine bug 候选（设计文档承诺但路径破了）
│   └─ 看 chatflow：worker 调对了工具但 DB 没改？
│       → 工具实现 bug（少见）/ tau-bench 校验 bug
├─ docker / llama-server / curl 起不来？
│   └─ infra 问题（不属于 Agentloom）
└─ Agentloom 答了但内容错（幻觉 / 自相矛盾）？
    └─ model quality 观察
```

**engine bug 候选** = 优先；**model quality** = 记录但不 block；**infra** = 修了重跑当前 task。

### D3. 捕获 — 每个 issue 要带的最小证据包

写 issue 时每条至少要含下面这些（不全的话调试者得反复回来问，效率低）：

```markdown
## Issue N: <一句话总结>

- **Category**: engine_bug | model_quality | infra | test_setup
- **Severity**: blocker | regression | minor | observation
- **Task**: 1 | 2 | 3 | 4 | preflight
- **Status**: detected | partial_repro | confirmed
- **Found at**: <YYYY-MM-DD HH:MM>

### Where
- chatflow_id: `<id 全长，方便 GET /api/chatflows/<id>>`（如有）
- chatnode_id: `<id>`（如有）
- worknode_id: `<id>`（如有）
- log path: `runs/.../run.log` line ~N
- guessed source: `backend/agentloom/<file>:<line>`（你能猜就猜，不能就空）

### Symptom（看到什么）
1-3 句话。原文 / 原 log 截选优先于你的转述。

### Expected（应该看到什么）
1-2 句话。如果你不确定 expected，就写"unknown — 不知道哪样算对"。

### Minimal repro
最短能复现的命令序列。如果靠运气只触发一次，写"flaky, did not isolate"。

### Hypothesis（你的初步猜测）
1-3 行。**不需要对**——一个错的猜测也比"不知道"信息量高。

### Attached evidence
- log excerpt: `runs/.../foo.log` line 123-145（贴 5-30 行原文）
- chatflow snapshot: `runs/.../bug-chatflow-N.json`（如果有可保留的状态）
- WorkFlow worknodes.tsv 截行（如果工作流路径异常）

### What debugger should check first
（你的 best guess：哪个文件 / 哪个 commit / 哪个测试该读）
```

### D4. 汇总到一个 handoff 文件

测试全部跑完（或决定 abort）后，**把所有 issue 写到一个文件**：

```bash
# 路径就这一个，别分散：
/home/usingnamespacestc/Agentloom/runs/issues-for-debug.md
```

文件结构：

```markdown
# 待 debug 问题清单 — <YYYY-MM-DD>

## 元信息
- 执行 AI: <你的标识，如 "Claude Sonnet 4.6 / Cursor / Aider …">
- backend commit: <`git rev-parse HEAD`>
- 测试模型: qwen36-27b-q4km @ localhost llama.cpp（json_mode=schema）
- 测试整体时长: <小时>
- 完成的 task: 1 / 2 / 3 / 4（哪几个跑完了）
- 中断原因（如有）: ...

## 摘要

| 指标 | 数 |
|---|---|
| 总 issue 数 | N |
| engine_bug 候选 | N |
| model_quality 观察 | N |
| infra 问题 | N |
| test_setup 问题 | N |

## 优先级建议（你给的，不用对）

- **P0 (block 后续测试)**: issue #X, #Y
- **P1 (回归 / 设计文档承诺破了)**: ...
- **P2 (minor / observation)**: ...

## Issue 详情

（按 D3 schema 一个个写，编号 1, 2, 3 ...）

## 此外的观察（不构成 issue 但值得提）
- 模型行为模式（如 qwen36 多 turn 后倾向输出停止）
- 性能数字（avg LLM call 时长 / GPU 利用率峰值）
- 测试覆盖盲区（你发现某个 feature 的测试我们都没写）
```

### D5. 怎么把 issue 抛给我（debugger）

执行 AI 写完 `runs/issues-for-debug.md` 后，告诉用户："写完了，路径 `runs/issues-for-debug.md`，请把内容贴给原作者 / debug AI"。

用户把那个文件 `cat` / 复制粘贴回 debug AI，debug AI 就能：
1. 直接 `Read` 提到的 chatflow JSON
2. `curl /api/chatflows/<id>` 看持久化状态（chatflow 没删过！这就是为什么铁则要求 keep）
3. `git show <commit>:<file>` 看相关源码
4. 跑 minimal repro 确认

### D6. 不确定是 bug 还是 model quality 怎么办

**两条都写**——分开两个 issue 不矛盾：
- 一个标 `Category: engine_bug` `Status: detected`，写 hypothesis
- 一个标 `Category: model_quality` `Status: observation`

debug AI 收到后会决定哪个更可能。你不需要 commit 一个判断。

### D7. 一个具体例子（参考）

如果 task 4 retail 有个 task reward=0 + status=failed 而你猜是 engine bug：

```markdown
## Issue 3: tau-bench retail task 7 在 worker 阶段 halt，疑似 capability_request 反馈循环没生效

- **Category**: engine_bug
- **Severity**: regression
- **Task**: 4
- **Status**: detected
- **Found at**: 2026-04-30 03:42

### Where
- chatflow_id: `019dd9aa-3322-71e0-8ff6-deadbeef1234`
- chatnode_id: `019dd9aa-...-turn3` （第 3 个 user turn 的 ChatNode）
- worknode_id: `019dd9aa-...-worker1`
- log path: `runs/tau-bench/retail-auto-plan/task_7.json` + `run.log:18432`
- guessed source: `backend/agentloom/engine/chatflow_engine.py::_after_worker_judge`

### Symptom
worker 节点输出含 `<capability_request>tau_xxx_modify_pending_order_items</capability_request>`，但
worker_judge 节点的 verdict.capability_escalation 是 []，没把 marker 升级。

### Expected
worker_judge 应该读 worker.capability_request → 写入 capability_escalation → 引擎 widen
inheritable_tools + spawn 新 planner（按 commit 2f9998f 设计）。

### Minimal repro
```bash
agentloom-bench --domain retail --task-ids 7 \
  --execution-mode auto_plan ...（跟 4.3 一样）
```

### Hypothesis
worker_judge fixture 模板里 `worker_capability_request` 变量替换可能有问题；或者 qwen36 没按 schema
写 capability_escalation。看 worker_judge 节点的 input_messages（system message 里 capability_request
是不是真嵌进来了）。

### Attached evidence
- log excerpt: `runs/tau-bench/retail-auto-plan/run.log` line 18432-18510（贴出来）
- chatflow snapshot: `curl /api/chatflows/019dd9aa-... > runs/issues/issue-3-chatflow.json`

### What debugger should check first
- `tests/backend/unit/test_capability_escalation_feedback.py` 是否真覆盖这条路径
- `backend/agentloom/templates/fixtures/zh-CN/worker_judge.yaml` 模板渲染
- worker 节点的 `capability_request` 字段是否真填上（看 chatflow.json）
```


1. **跑顺序**：建议 task 1 → task 2 → task 3 → task 4。task 1 验证整套测试基础工作，前面没跑通后面别开始。
2. **task 4 是耗时大头**：可能要 10-20 小时，**起 nohup 后定期 check log**，不要一直堵在前台。
3. **每个 task 完整 self-contained**：写 summary 之前不要开始下一个 task。
4. **遇到 backend 异常 / qwen36 OOM / GPU 卡死**：先记录症状到当前 task 的 summary，然后判断是否能继续（重启 llama-server / 缩小 ctx-size 等）。
5. **GPU 监控**：`nvidia-smi -l 5` 在另一个 terminal 跑着，task 4 期间留一个 GB 的 headroom，否则 decode 速度掉 15×。
6. **不要清理！** 所有 chatflow / 测试输出 / log 都保留。我会回来 drill-in。
7. **时间预算**：task 1 ~2-3h；task 2 ~1.5-3h（8-10 turn × 5-15min）；task 3 ~1-2h（6 turn）；task 4 ~10-20h。**整套预计 15-30 小时**——qwen36 + json_mode 慢是常态，不要因为"看着没动"就 kill。

完事请把每个 task 的 `summary.md` 路径列在最终回报里。
