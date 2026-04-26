"""τ-bench session lifecycle + tool wrapper module.

Three layers:

- :mod:`.tool_source` — :class:`TauBenchToolWrapper` adapts a tau_bench
  tool class to Agentloom's :class:`Tool` ABC; :class:`TauBenchToolSource`
  bundles all wrappers for one session's domain (retail / airline)
  with the session's mock DB and tracks them for unregister.

- :mod:`.runtime` — process-level registry of active sessions. Mirrors
  :mod:`agentloom.mcp.runtime`: ``add_session`` / ``remove_session`` /
  ``get_session`` with the shared :class:`ToolRegistry` mutated under
  the hood.

- :mod:`.api` — FastAPI router exposing ``POST /api/tau-bench/sessions``
  and ``POST /api/tau-bench/sessions/{id}/teardown`` for the runner.

Backend never imports the upstream tau_bench user simulator (litellm
pulls in too much weight); the vendored tau_bench under
``backend/vendor/tau_bench/`` is the only tau_bench source that backend
loads. See ``backend/vendor/tau_bench/PATCHES.md`` for the vendor
patches that make the litellm-free backend import path possible.
"""
