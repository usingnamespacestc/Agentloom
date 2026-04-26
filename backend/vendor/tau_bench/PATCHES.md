# Agentloom vendor patches against tau-bench upstream

Vendor source: https://github.com/sierra-research/tau-bench
Pinned at: `59a200c6d575d595120f1cb70fea53cef0632f6b` (2026-03-18, "Merge pull request #80 from sierra-research/update-readme-tau3-bench")
License: MIT (see LICENSE)

## What we vendor

The full `tau_bench` package tree EXCEPT `tau_bench/agents/`, `tau_bench/model_utils/`, and `tau_bench/run.py`. Those layers depend on litellm + openai SDK and are never imported by Agentloom backend (the runner installs the full upstream package separately).

## What we patched

### 1. `vendor/tau_bench/envs/user.py`

**Why**: upstream does `from litellm import completion` at module top. The
`envs/__init__.py` and `envs/base.py` both import from `user.py` at module
top, which means importing **any retail or airline tool** transitively
requires litellm. Agentloom backend doesn't use the LLM user simulator (the
runner does, in its own conda env), so we wrap the litellm import in
`try/except ImportError` and set `completion = None` as a degraded fallback.

**Patch (one block at module top)**:
```python
try:
    from litellm import completion
except ImportError:  # pragma: no cover - vendor patch for backend zero-litellm install
    completion = None  # type: ignore[assignment]
```

**Risk**: if Agentloom backend ever directly calls `LLMUserSimulationEnv`
or `ReactUserSimulationEnv` etc. without litellm installed, it will hit
`AttributeError: 'NoneType' object has no attribute ...` at the
`completion(...)` call site. By design, backend never instantiates these —
the runner does, in its own env that pip-installs upstream tau_bench (which
brings litellm).

### 2. `vendor/tau_bench/__init__.py`

**Why**: upstream re-exports `Agent` at the top level:
```python
from tau_bench.agents.base import Agent as Agent
```
We don't vendor `tau_bench/agents/` (the upstream agent loops, which
depend on litellm + openai SDK and are never imported by Agentloom).
Without dropping the re-export, even `import tau_bench` raises
`ModuleNotFoundError: No module named 'tau_bench.agents'`.

**Patch**: delete the `from tau_bench.agents.base import Agent` line.
Backend code that needs an agent uses Agentloom's own ChatFlow runtime;
it never imports upstream `Agent`.

## Sync regimen

Periodic sync (manual; benchmark schemas change rarely):

1. `git clone --depth 1 git@github.com:sierra-research/tau-bench /tmp/tau-bench-upstream`
2. `cd /tmp/tau-bench-upstream && git checkout <new-sha>`
3. Diff: `diff -ru /tmp/tau-bench-upstream/tau_bench backend/vendor/tau_bench` (excluding patched files)
4. Re-apply this PATCHES.md's patch list (currently 2 patches — `envs/user.py` litellm try-import + top-level `__init__.py` Agent re-export drop)
5. Update `Pinned at:` line above with the new sha + date
6. Run smoke tests in both backend and runner conda envs

If any patched file changes upstream, the diff will surface conflicts —
re-apply the patch to the new file.
