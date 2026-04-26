"""τ-bench runner package.

Exports the two main building blocks that the PR 4 CLI will compose:

- :class:`TauBenchBackendClient` — async HTTP client wrapping the
  Agentloom backend's ``/api/tau-bench/sessions`` + ``/turns`` +
  ``/teardown`` endpoints.
- :class:`TauBenchRunner` — single-task driver that runs a multi-turn
  user_simulator ↔ backend loop to completion, returns a
  :class:`TaskTrace`.

Tool source / adapter (the upstream tau_bench ``UserStrategy`` wrapper)
lands in PR 4. Report aggregation in PR 5. CI cron in PR 6.
"""
from .client import SessionInfo, TauBenchBackendClient, TurnResult
from .runner import TaskTrace, TauBenchRunner, TurnRecord, UserSimulator

__all__ = [
    "SessionInfo",
    "TaskTrace",
    "TauBenchBackendClient",
    "TauBenchRunner",
    "TurnRecord",
    "TurnResult",
    "UserSimulator",
]
