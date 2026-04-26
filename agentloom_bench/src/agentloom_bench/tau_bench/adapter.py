"""Adapter wrapping upstream ``tau_bench.envs.user.UserStrategy`` LLM
implementations to satisfy the runner's :class:`UserSimulator`
Protocol.

Lives in the runner's conda env (``agentloom-bench``) where upstream
tau_bench is fully installed (litellm + the LLM strategies). The
runner itself doesn't import this module unless real LLM-driven user
simulation is wired in — tests use plain stubs.
"""
from __future__ import annotations

from typing import Any


def build_user_simulator(
    *,
    user_model: str,
    user_strategy: str = "llm",
    user_provider: str | None = None,
) -> Any:
    """Construct a τ-bench user simulator instance.

    ``user_strategy`` is one of {"llm", "react", "verify", "reflection",
    "human"} per upstream. Default ``"llm"`` matches the paper's
    baseline. ``user_provider`` is the litellm provider prefix (e.g.
    ``"volcengine"`` for ark, ``"anthropic"`` for sonnet); ``None``
    lets litellm guess from ``user_model``.

    Returns an object with ``reset(instruction) -> str`` and
    ``step(content) -> str``, satisfying :class:`UserSimulator`.
    """
    from tau_bench.envs.user import load_user

    return load_user(
        user_strategy=user_strategy,
        model=user_model,
        provider=user_provider,
    )
