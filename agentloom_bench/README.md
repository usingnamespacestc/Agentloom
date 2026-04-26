# agentloom-bench

Out-of-process benchmark harness for Agentloom. Drives external agentic
benchmarks (currently just τ-bench; BFCL / SWE-bench planned) against a
running Agentloom backend over HTTP.

## Status: PR 1 of 6

Skeleton only — runner lands in PR 4. See
[`docs/design-tau-bench-integration.md`](../docs/design-tau-bench-integration.md)
for the full plan.

## Why a separate distribution

Two reasons:

1. **Dependency isolation** — `tau_bench` upstream pulls `litellm`, which
   has heavy transitive deps and version pins that conflict with the
   Agentloom backend's curated dependency set. Running benchmarks in a
   separate conda env keeps both clean.
2. **Process boundary** — runner talks to the backend over HTTP exactly
   the way a real client would, so latency / failure modes / SSE behavior
   all stay realistic. Same pattern will scale to BFCL / SWE-bench later.

The Agentloom backend ships a **vendored subset** of `tau_bench` (under
`backend/vendor/tau_bench/`, MIT, see `PATCHES.md` there) so it can
import retail / airline tools to register them into the tool registry —
without pulling litellm into the backend env. The runner here installs
the full upstream package so it can use `UserStrategy` (the LLM-driven
simulated user) and `env.calculate_reward()` (ground-truth reward
computation).

Both sides pin the same upstream sha — `59a200c6d575d595120f1cb70fea53cef0632f6b`
— so the tool implementations the backend simulates and the reward
hashes the runner computes match exactly. Sync regimen lives in
`backend/vendor/tau_bench/PATCHES.md`.

## Setup

```bash
conda create -n agentloom-bench python=3.12 -y
conda activate agentloom-bench
cd agentloom_bench
pip install -e ".[dev]"
```

The `pip install` will pull `tau_bench` from GitHub at the pinned sha.
First install takes ~2 min (litellm cascade is heavy).

## Smoke test

```bash
pytest tests/smoke -v
```

Three tests:

1. `test_agentloom_bench_importable` — package skeleton works
2. `test_upstream_tau_bench_importable_with_litellm` — runner env has full
   upstream tau_bench + litellm, confirms vendor subset isn't shadowing
3. `test_upstream_retail_tasks_match_pinned_sha` — task counts (retail
   115, airline 50) match what backend vendor sees, guarding against
   silent upstream drift between the runner's git+ install and the
   backend's vendored copy

If test 3 fails, either:
- runner env's tau_bench was installed from a different sha than
  `backend/vendor/tau_bench/PATCHES.md` says
- upstream changed task counts (rare — schemas are stable)

Re-pin to a consistent sha before continuing.
