"""Benchmark adapters that register external benchmark tools (tau-bench,
BFCL, ...) into Agentloom's shared tool registry on a per-session basis.

This package is intentionally **passive**: the benchmark runner driver
lives outside the backend (in ``agentloom_bench/``, a separate
distribution). The runner makes HTTP requests to the backend's
benchmark API endpoints to spin a session up, submit user turns, then
tear it down. See ``docs/design-tau-bench-integration.md``.
"""
