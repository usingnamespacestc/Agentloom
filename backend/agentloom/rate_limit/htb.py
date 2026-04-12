"""Hierarchical Token Bucket implementation.

Design notes
------------
* Buckets form a tree; acquiring a leaf walks to the root, acquiring each
  ancestor in order. Release unwinds in reverse so we never leak slots.
* All dimensions share one ``acquire`` path. If any dimension is blocked
  the coroutine sleeps on an ``asyncio.Condition`` tied to that bucket and
  is retried when a neighbor releases or when a refill tick fires.
* Time is fully injectable via ``clock: Callable[[], float]``. Tests use a
  synthetic clock to simulate hours of window-based activity in milliseconds
  of wall time.
* No global state, no threads, asyncio-only. One bucket tree per process
  for MVP; distribution across workers is a post-MVP concern.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

import yaml


@dataclass
class BucketConfig:
    """Static configuration for one bucket."""

    name: str
    parent: str | None = None
    concurrent: int | None = None  # max in-flight requests
    rpm: float | None = None  # tokens per 60s (float for fractional)
    sliding_window_requests: int | None = None  # cap within window
    sliding_window_seconds: float | None = None  # the window length
    description: str = ""


class Bucket:
    """One node in the bucket tree."""

    def __init__(self, config: BucketConfig, clock: Callable[[], float]) -> None:
        self.config = config
        self._clock = clock

        self._concurrent_in_use = 0
        self._condition = asyncio.Condition()

        # RPM token bucket state
        if config.rpm is not None:
            self._rpm_capacity = max(1.0, float(config.rpm))
            self._rpm_tokens = self._rpm_capacity
            self._rpm_refill_rate_per_sec = float(config.rpm) / 60.0
            self._rpm_last_refill = clock()
        else:
            self._rpm_capacity = 0.0
            self._rpm_tokens = 0.0
            self._rpm_refill_rate_per_sec = 0.0
            self._rpm_last_refill = 0.0

        # Sliding window state
        self._window_events: deque[float] = deque()

    @property
    def name(self) -> str:
        return self.config.name

    def _refill_rpm(self) -> None:
        if self.config.rpm is None:
            return
        now = self._clock()
        elapsed = now - self._rpm_last_refill
        if elapsed <= 0:
            return
        self._rpm_tokens = min(
            self._rpm_capacity, self._rpm_tokens + elapsed * self._rpm_refill_rate_per_sec
        )
        self._rpm_last_refill = now

    def _prune_window(self) -> None:
        if self.config.sliding_window_seconds is None:
            return
        cutoff = self._clock() - self.config.sliding_window_seconds
        while self._window_events and self._window_events[0] < cutoff:
            self._window_events.popleft()

    def _can_admit(self) -> tuple[bool, float]:
        """Return ``(ok, next_retry_after_seconds)``.

        ``next_retry_after_seconds`` is a best-effort hint (not a tight bound).
        If not admissible, ``0`` means "wake on neighbor release"; a positive
        number means "wake at least this soon because of time-based refill".
        """
        # Concurrent
        if self.config.concurrent is not None and self._concurrent_in_use >= self.config.concurrent:
            return (False, 0.0)

        # RPM
        if self.config.rpm is not None:
            self._refill_rpm()
            if self._rpm_tokens < 1.0:
                needed = 1.0 - self._rpm_tokens
                eta = needed / self._rpm_refill_rate_per_sec if self._rpm_refill_rate_per_sec else 0
                return (False, max(eta, 0.001))

        # Sliding window
        if self.config.sliding_window_requests is not None and self.config.sliding_window_seconds:
            self._prune_window()
            if len(self._window_events) >= self.config.sliding_window_requests:
                # Retry when the oldest event falls out of the window
                oldest = self._window_events[0]
                eta = (oldest + self.config.sliding_window_seconds) - self._clock()
                return (False, max(eta, 0.001))

        return (True, 0.0)

    def _commit(self) -> None:
        """Charge this bucket for one granted acquisition."""
        if self.config.concurrent is not None:
            self._concurrent_in_use += 1
        if self.config.rpm is not None:
            self._rpm_tokens -= 1.0
        if self.config.sliding_window_requests is not None:
            self._window_events.append(self._clock())

    def _release_concurrent(self) -> None:
        if self.config.concurrent is not None:
            self._concurrent_in_use = max(0, self._concurrent_in_use - 1)

    # Introspection helpers used in tests and the UI
    def stats(self) -> dict[str, Any]:
        self._refill_rpm()
        self._prune_window()
        return {
            "name": self.name,
            "concurrent_in_use": self._concurrent_in_use,
            "concurrent_limit": self.config.concurrent,
            "rpm_tokens": round(self._rpm_tokens, 3) if self.config.rpm is not None else None,
            "rpm_limit": self.config.rpm,
            "window_in_use": len(self._window_events)
            if self.config.sliding_window_requests is not None
            else None,
            "window_limit": self.config.sliding_window_requests,
            "window_seconds": self.config.sliding_window_seconds,
        }


class BucketTree:
    """Container for a set of buckets forming a forest (typically one root).

    Resolving the chain for a given leaf walks parents until ``None``. Acquire
    locks each bucket's condition and admits per §_can_admit.
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._buckets: dict[str, Bucket] = {}

    def add(self, config: BucketConfig) -> Bucket:
        if config.name in self._buckets:
            raise ValueError(f"duplicate bucket name: {config.name}")
        if config.parent is not None and config.parent not in self._buckets:
            raise ValueError(
                f"parent {config.parent!r} must be added before child {config.name!r}"
            )
        # Reject cycles (can't happen given the declaration-order rule but
        # let's be explicit so future refactors don't regress).
        parent_walk = config.parent
        while parent_walk is not None:
            if parent_walk == config.name:
                raise ValueError(f"cycle detected at {config.name}")
            parent_walk = self._buckets[parent_walk].config.parent
        bucket = Bucket(config, self._clock)
        self._buckets[config.name] = bucket
        return bucket

    def get(self, name: str) -> Bucket:
        return self._buckets[name]

    def chain(self, leaf_name: str) -> list[Bucket]:
        """Return [leaf, ..., root]. Order matters for lock acquisition."""
        if leaf_name not in self._buckets:
            raise KeyError(leaf_name)
        out: list[Bucket] = []
        name: str | None = leaf_name
        while name is not None:
            b = self._buckets[name]
            out.append(b)
            name = b.config.parent
        return out

    def stats(self) -> list[dict[str, Any]]:
        return [b.stats() for b in self._buckets.values()]


class RateLimiter:
    """Public API: ``async with limiter.acquire(leaf_name): ...``"""

    def __init__(self, tree: BucketTree) -> None:
        self._tree = tree

    def acquire(self, leaf_name: str) -> "_Acquisition":
        return _Acquisition(self._tree, leaf_name)

    def stats(self) -> list[dict[str, Any]]:
        return self._tree.stats()


class _Acquisition:
    """Async context manager that acquires a full parent chain atomically.

    "Atomically" here means: we either commit all ancestors together or none.
    Released slots are freed in reverse order so we never double-commit.
    """

    def __init__(self, tree: BucketTree, leaf_name: str) -> None:
        self._tree = tree
        self._leaf_name = leaf_name
        self._chain: list[Bucket] = []

    async def __aenter__(self) -> None:
        chain = self._tree.chain(self._leaf_name)
        # Try each bucket in order. If any blocks, roll back the ones we
        # already committed (release concurrency) and wait.
        while True:
            committed: list[Bucket] = []
            blocked_on: Bucket | None = None
            block_hint: float = 0.0
            for bucket in chain:
                async with bucket._condition:
                    ok, hint = bucket._can_admit()
                    if not ok:
                        blocked_on = bucket
                        block_hint = hint
                        break
                    bucket._commit()
                    committed.append(bucket)
            if blocked_on is None:
                self._chain = chain
                return
            # Roll back partial commits
            for b in reversed(committed):
                async with b._condition:
                    b._release_concurrent()
                    b._condition.notify_all()
            # Wait for the blocking bucket
            async with blocked_on._condition:
                if block_hint > 0:
                    try:
                        await asyncio.wait_for(blocked_on._condition.wait(), timeout=block_hint)
                    except asyncio.TimeoutError:
                        pass
                else:
                    await blocked_on._condition.wait()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # Release concurrency slots on the whole chain, in reverse order.
        for bucket in reversed(self._chain):
            async with bucket._condition:
                bucket._release_concurrent()
                bucket._condition.notify_all()
        self._chain = []


# ------------------------------------------------------------------ YAML loader


def load_bucket_tree_from_yaml(
    source: str | dict[str, Any],
    *,
    clock: Callable[[], float] | None = None,
) -> BucketTree:
    """Build a :class:`BucketTree` from YAML text or a parsed dict.

    Schema::

        buckets:
          - name: global
            concurrent: 64
          - name: provider:volcengine
            parent: global
            sliding_window_requests: 1000
            sliding_window_seconds: 18000   # 5 hours
            concurrent: 2
            rpm: 3
            description: "Coding Plan Lite"
    """
    data: dict[str, Any]
    if isinstance(source, str):
        loaded = yaml.safe_load(source)
        if not isinstance(loaded, dict):
            raise ValueError("bucket YAML must map to an object at the top level")
        data = loaded
    else:
        data = source

    tree = BucketTree(clock=clock)
    entries = data.get("buckets", [])
    if not isinstance(entries, list):
        raise ValueError("`buckets` must be a list")

    for raw in entries:
        if not isinstance(raw, dict):
            raise ValueError("each bucket entry must be a mapping")
        config = BucketConfig(
            name=str(raw["name"]),
            parent=raw.get("parent"),
            concurrent=raw.get("concurrent"),
            rpm=raw.get("rpm"),
            sliding_window_requests=raw.get("sliding_window_requests"),
            sliding_window_seconds=raw.get("sliding_window_seconds"),
            description=str(raw.get("description", "")),
        )
        tree.add(config)

    return tree
