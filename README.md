# Agentloom

> Visual agent workflow platform — every conversation is a branchable DAG,
> every agent step is inspectable, editable, and replayable.

Agentloom is a thinking-environment for LLM-powered agents. Instead of a
linear chat log or a hand-wired workflow, the conversation itself is a
DAG you can branch, merge, zoom into, and replay.

- **ChatFlow** — the outer DAG of user-turn + agent-turn pairs.
- **WorkFlow** — the inner DAG of model calls, tool calls and sub-agent
  delegations inside each ChatFlow node.
- **Plan / Execute** — two phases of every node (dashed → solid), not
  separate UI modes.
- **Immutable on execute** — solid nodes are frozen; iteration happens by
  branching, not by overwriting.

Full design lives in `docs/` (local-only, not committed).

## Dev setup

```bash
cp .env.example .env
# fill in VOLCENGINE_API_KEY, TAVILY_API_KEY, ANTHROPIC_API_KEY as available

# start postgres + redis
docker compose up -d postgres redis

# backend (hot reload)
cd backend
pip install -e ".[dev]"
uvicorn agentloom.main:app --reload

# in another terminal: frontend
cd frontend
npm install
npm run dev
```

Health check: `curl localhost:8000/health` → `{"status":"ok","version":"0.1.0"}`
Frontend: open `http://localhost:5173`.

## Tests

```bash
make test           # backend unit + integration
make test-smoke     # live API tests (requires env keys)
make test-e2e       # playwright
```

All test code lives in `tests/` (local-only, not committed).

## Layout

```
backend/
  agentloom/
    api/          HTTP routes
    db/           models + repositories
    engine/       WorkFlow execution engine
    providers/    OpenAI-compat / Anthropic native adapters
    tools/        Bash / Read / Write / Edit / Glob / Grep
    mcp/          MCP client
    system_workflows/  merge, compact, plan_elaborate, title_gen
    rate_limit/   Hierarchical Token Bucket
frontend/
  src/
    canvas/       React Flow + WorkFlow panel
    i18n/         zh-CN + en-US locales
    store/        Zustand stores
```

## Status

Early development. See `docs/plan.md` (local) for the phased build plan
(M0 → M13 for MVP, M14+ for v1.1/v1.2 features).
