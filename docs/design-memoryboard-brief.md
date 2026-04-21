# MemoryBoard — brief WorkNode foundation

> Design doc for the 2026-04-20 overnight run.
> Scope split across 3 PRs (this PR = foundation; PR 2 = read-path migration + frontend polish + Postgres indices; PR 3 = ChatBoard cascading).
> Terminology follows `project_agentloom_memoryboard_terms.md` and `feedback_agentloom_node_kinds.md`.

---

## 0. One-paragraph summary

MemoryBoard is the unifying data model that will eventually subsume `blackboard`, `CompactSnapshot`, `MergeSnapshot`, and the `get_node_context` skill into one searchable / forgetting / tiered store of structured items. The ChatFlow-layer view is called **ChatBoard** (one row per ChatNode); the WorkFlow-layer view is called **WorkBoard** (one row per WorkNode). Each entry is a **MemoryBoardItem** (`ChatBoardItem` / `WorkBoardItem`). The first stone this PR sets is **brief** — a new Layer-1 WorkNode kind that distills each source node into a single short description that populates that node's MemoryBoardItem row. brief is the **producer**; reader skills and frontend bubbles in later PRs are the **consumers**.

---

## 1. Naming lock-in (from memory, not negotiable this run)

### WorkNode two-layer taxonomy

Layer 1 (pipeline stage, one of):
`pre-check` · `planning` · `execution` · `monitoring` · `post-check` · `compress` · `brief` · `pack` (future)

Layer 2 (execution form, one of):
`draft` (was `llm_call`) · `tool_call` · `delegate` (was `sub_agent_delegation`)

### Legacy renames (engine still reads old names during migration window, PR 2 flips)

- `llm_call` → `draft`
- `planner` → `plan`
- `sub_agent_delegation` → `delegate`
- `default_model` (on ChatFlow / WorkflowSettings) → `draft_model`

### This PR's new kinds

- `StepKind.BRIEF` — source-node summarizer; produces one MemoryBoardItem.
- (Already existing under old name, no rename this PR:) `compact` ↔ `compress`. We keep `compact` in code this PR; PR 2 flips the name alongside the reader migration.

---

## 2. brief — core semantics

### 2.1 Purpose

A `brief` WorkNode takes **one source node** and writes a single short prose description capturing:
- **What the source did** (one-sentence intent)
- **What it produced** (the takeaway a downstream reader actually needs)

That text becomes the source node's `MemoryBoardItem.description`. Nothing else derives from it this PR; PR 2 wires the reader skill.

### 2.2 Two scopes: node-brief vs flow-brief

Both are `StepKind.BRIEF`, distinguished by a new `scope: NodeScope` field on `WorkFlowNode`:

| scope       | source                  | input                                                                 | writes to                     |
|-------------|-------------------------|-----------------------------------------------------------------------|-------------------------------|
| `NODE`      | one sibling WorkNode    | that WorkNode's own inputs + outputs                                  | that WorkNode's WorkBoardItem |
| `FLOW`      | enclosing WorkFlow      | pre-check upstream context + all node-briefs concatenated + post-check output | the enclosing WorkFlow's WorkBoardItem (the delegate's own "node-brief" at the parent layer) |

**Delegate asymmetry (decision locked 2026-04-19):** a `delegate` WorkNode does **not** get its own node-brief. Instead, the inner WorkFlow's `flow-brief` plays that role — when the parent WorkFlow later reads this delegate's WorkBoardItem, the text it sees is the sub-WorkFlow's flow-brief output.

### 2.3 brief does not need its own brief (recursion guard)

brief nodes are skipped by the auto-spawn rule. Compact / merge / judge / plan nodes each get a node-brief; brief does not.

### 2.4 Synchronous, downstream blocks

When a node succeeds, the engine spawns its brief **synchronously** (the brief is placed as a new WorkNode whose `parent_ids = [source_node_id]`; it is excluded from the main-axis ready set so siblings can still parallelize around it, but the WorkFlow only considers itself "terminally done" once every in-flight brief has resolved). flow-brief fires last, when the WorkFlow hits a terminal state (succeeded, halted, or cancelled — all three).

### 2.5 tool_call source uses code, not LLM

Running a brief LLM call over every tool_call's raw output would explode cost in ReAct loops. `run_brief(source)` dispatches:

- if `source.step_kind == TOOL_CALL`: assemble a deterministic 1–2-line code template (tool name + truncated first line of result + error flag). Skip LLM entirely.
- otherwise: LLM call using `node_brief.yaml` / `flow_brief.yaml` fixture, with `brief_model` pin.

### 2.6 Failure path

If the LLM brief call fails (retry exhausted), fall back to the same code template used for tool_call and set a `fallback: True` flag on the resulting MemoryBoardItem. A failed brief never fails the source WorkNode — the source stays succeeded; the brief WorkNode reports status=succeeded with the fallback text.

### 2.7 brief_model pin + context_window invariant

ChatFlow gets a new `brief_model: ProviderModelRef | None` field (alongside the existing `judge_model` / `tool_call_model`). Pydantic validator: if set, `brief_model`'s context window must be **≥ draft_model's context window**. Reason: a flow-brief must fit the full upstream context of any draft node it summarizes, so picking a smaller window for brief would silently truncate.

---

## 3. MemoryBoardItem schema (this PR: minimal viable shape)

`board_items` table, new this PR:

| column             | type                    | notes                                                              |
|--------------------|-------------------------|--------------------------------------------------------------------|
| `id`               | `String(64)` PK         | UUIDv7                                                             |
| `workspace_id`     | `String(64)` FK         | tenancy                                                            |
| `chatflow_id`      | `String(64)` FK         | present for both ChatBoard + WorkBoard items                       |
| `workflow_id`      | `String(64)` nullable   | null for ChatBoardItem; set for WorkBoardItem                      |
| `source_node_id`   | `String(64)`            | the ChatNode or WorkNode this item summarizes                      |
| `source_kind`      | `String(32)`            | source node kind ("draft" / "tool_call" / "judge_call" / "merge" / "compact" / "chat_turn" / "flow" / ...) |
| `scope`            | `String(16)`            | "chat" / "node" / "flow"                                           |
| `description`      | `Text`                  | the short prose; **single field, not split** (locked 2026-04-19)   |
| `produced_tags`    | `JSONB` default `[]`    | reserved — future labeled tags for search precision                |
| `consumed_tags`    | `JSONB` default `[]`    | reserved                                                           |
| `fallback`         | `Boolean` default false | True if generated by code fallback (LLM failed or tool_call)       |
| `created_at`       | `DateTime(tz=true)`     |                                                                    |
| `updated_at`       | `DateTime(tz=true)`     |                                                                    |

Indices: `(workspace_id, chatflow_id, scope)` for lookup by ChatFlow; `(source_node_id)` for direct address. Postgres tsvector / pg_trgm / pgvector indices land in PR 2.

### 3.1 Why single `description`, not split into input/output (user decision)

Asked between "split into `input_description` + `output_description`" vs "single `description`", user chose single. Reasoning:

1. description is a **brief**, not a schema — it reads like a sentence, not a form.
2. The node's raw inputs/outputs are still on the WorkNode itself. When precision is needed, downstream tools walk to the node directly.
3. Search precision will be solved in PR 2 via labeled tag fields (`produced_tags` / `consumed_tags`) with their own vector / trigram indices.
4. Schema unification: ChatBoardItem and WorkBoardItem share the same row shape, so migration is easier.

---

## 4. Engine changes (this PR)

### 4.1 Schema layer

`schemas/common.py`:
- Add `StepKind.BRIEF = "brief"`.
- Add `NodeScope(str, Enum)` with `NODE = "node"` and `FLOW = "flow"`. (New file-local enum in `common.py` since it's shared by both schemas and DB.)

`schemas/workflow.py`:
- Add `scope: NodeScope | None = None` to `WorkFlowNode`. Required when `step_kind == BRIEF`, forbidden otherwise.
- Add to `_validate_step_kind_fields`: brief nodes carry only `input_messages` / `output_message` / `usage` (same as llm_call / compact).
- Add `brief_model_override: ProviderModelRef | None = None` to `WorkFlow` (parallel to `judge_model_override` / `tool_call_model_override`).

`schemas/chatflow.py`:
- Add `brief_model: ProviderModelRef | None = None` to `ChatFlow` alongside `default_model` → (renamed to) `draft_model`.
- Add pydantic model_validator: `brief_model`'s resolved context_window ≥ `draft_model`'s (looked up via the existing `provider_context_cache`). If lookup fails (unknown model), validator passes silently — we don't want startup failures on unknown provider metadata.

### 4.2 WorkflowEngine

New method `_run_brief(node, workflow)`:
- Reads `source_node_id` from `node.parent_ids[0]`.
- Dispatches: tool_call path uses code template; other kinds use `node_brief.yaml` or `flow_brief.yaml` fixture.
- Writes a `BoardItem` row via a new `BoardItemRepository.upsert_by_source(...)` method. (Idempotent so retries don't duplicate.)
- On LLM failure, falls back to the code template and marks `fallback=True` on the item.
- Always sets its own status to SUCCEEDED (never propagates failure to source).

### 4.3 Auto-spawn points

Inside `WorkflowEngine._run_node`, **after** a non-brief node reaches terminal state SUCCEEDED, if:
- source is not a brief itself (recursion guard)
- source is not a delegate (delegate's brief is the inner flow-brief)

…spawn a brief WorkNode with `parent_ids=[source.id]`, `step_kind=BRIEF`, `scope=NODE`.

Inside `WorkflowEngine.run()` (or the terminal-state hook), when the WorkFlow itself reaches a terminal state (SUCCEEDED / halted via `pending_user_prompt` / cancelled), spawn one `scope=FLOW` brief whose parent_ids = every terminal node's id. Run it synchronously, then propagate workflow-level terminal.

### 4.4 Main-axis ready set exclusion

Brief nodes never block the downstream main work — they're off-axis. The "ready" predicate in scheduling skips brief nodes when computing ready-to-run main-axis nodes, so a WorkFlow with pending briefs doesn't look stuck. Briefs run in a secondary queue.

### 4.5 Fixtures

New `templates/fixtures/en-US/node_brief.yaml` + `templates/fixtures/zh-CN/node_brief.yaml`:
- One JSON-output llm_call node. Schema: `{"description": str}`.
- System prompt: "Summarize the following WorkNode in one or two sentences. State what it did and what it produced. Do not speculate about downstream use."

New `templates/fixtures/en-US/flow_brief.yaml` + `templates/fixtures/zh-CN/flow_brief.yaml`:
- Same shape, but the user message contains: pre-check upstream inputs + concatenated node-brief descriptions + post-check output.
- System prompt explains the WorkFlow-level framing.

---

## 5. Frontend changes (this PR)

Minimum viable:
- Rename i18n keys `default_model` → `draft_model` in both locales.
- ChatFlow settings modal: new row for `brief_model` dropdown below `tool_call_model`. Label: `简介模型` / `Brief model`. Helper text notes the context-window-must-be-at-least-draft constraint.
- Store + API types: `default_model` → `draft_model`; add `brief_model`.

Floating bubble + frame banner UI is **PR 2** scope, not this PR.

---

## 6. Test plan (this PR)

Unit tests to add under `tests/backend/unit/`:

1. `test_brief_spawn_on_success` — succeed an llm_call; assert a brief WorkNode appears with `parent_ids=[llm_call.id]`.
2. `test_brief_recursion_guard` — succeed a brief; assert no brief-of-brief is spawned.
3. `test_brief_scope_validator` — constructing a WorkFlowNode with `step_kind=BRIEF` without `scope` raises; non-BRIEF with `scope` raises.
4. `test_brief_tool_call_code_template` — succeed a tool_call; brief runs, no LLM call, text format is deterministic, `fallback=True`.
5. `test_brief_llm_failure_fallback` — monkeypatch LLM to raise; assert brief still succeeds with code template text + `fallback=True`.
6. `test_brief_flow_scope_spawn` — WorkFlow with 2 nodes reaches SUCCEEDED; assert exactly one `scope=FLOW` brief appears with `parent_ids` covering terminal nodes.
7. `test_brief_flow_spawn_on_halt` — WorkFlow halts via `pending_user_prompt`; flow-brief still fires.
8. `test_brief_delegate_has_no_node_brief` — delegate finishes; assert no `scope=NODE` brief spawned with delegate as parent (only the inner flow-brief exists).
9. `test_draft_model_rename` — ChatFlow JSON round-trip: old `default_model` loads, new `draft_model` saves.
10. `test_brief_model_context_window_invariant` — constructing a ChatFlow with `brief_model` whose window is smaller than `draft_model`'s raises; equal or bigger is ok; unknown-provider model passes silently.

Frontend: extend `chatflowStore.test.ts` and `ChatFlowCanvas.test.tsx` for the `draft_model` rename.

---

## 7. Data migration (rename `default_model`)

The ChatFlow row stores per-chatflow settings inside a JSONB `payload` column, so no DDL is needed. Alembic migration does one SQL UPDATE:

```sql
UPDATE chatflows
SET payload = jsonb_set(
    payload - 'default_model',
    '{draft_model}',
    payload -> 'default_model'
)
WHERE payload ? 'default_model';
```

Plus analogous for WorkspaceSettings if it has a top-level `default_model` key. Downgrade reverses.

---

## 8. Deferred to PR 2

- MemoryBoard reader skill (replaces `get_node_context`). Feature-flagged coexistence — old skill stays callable during migration, new one routes through `board_items`.
- `blackboard` / `CompactSnapshot` / `MergeSnapshot` read-path migration. Each becomes a view over `board_items`.
- `capabilities` field on WorkFlow (produced by judge_pre, consumed by plan), plus `assigned_resources` cropping on worker spawn.
- Frontend visual polish: floating bubble above WorkNode (node-brief); canvas-frame top banner (flow-brief).
- Postgres retrieval DDL: tsvector column + GIN; pg_trgm index on `description`; pgvector column + ivfflat index.
- `compact` → `compress` rename (fixture + code + UI).

## 9. Deferred to PR 3

- ChatBoard snapshot cascading inheritance: when a new ChatNode is appended, it inherits its parent's full ChatBoard plus a freshly-minted ChatBoardItem for the parent.

---

## 10. Out of scope for this whole three-PR series

- `pack` Layer-1 kind (future).
- Multi-index search (summary / logical / temporal) — we set the shape, don't implement retrieval.
- Three-tier detail levels (summary / structured / full) — same.
- "Judge打回 = reject tag on item" — same.
- MemoryBoard UI for browsing / editing items.
