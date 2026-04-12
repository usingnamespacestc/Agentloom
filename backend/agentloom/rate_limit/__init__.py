"""Hierarchical Token Bucket (HTB) rate limiting.

Every provider call is routed through a bucket tree. Each bucket can limit
on any combination of:

- **concurrent** — how many calls may be in-flight at once
- **rpm** — classic token bucket refilled at ``rpm/60`` tokens per second
- **sliding_window** — count of requests within the last ``period`` seconds

A child bucket must satisfy both its own limits AND every ancestor's. Acquire
is a single async context manager; on exit, concurrent slots are released
while the RPM/window counters persist naturally (rolling refill).

This module is deliberately self-contained and has no I/O: all timing is
driven by an injectable monotonic clock for deterministic unit tests.
"""

from agentloom.rate_limit.htb import (
    Bucket,
    BucketConfig,
    BucketTree,
    RateLimiter,
    load_bucket_tree_from_yaml,
)

__all__ = [
    "Bucket",
    "BucketConfig",
    "BucketTree",
    "RateLimiter",
    "load_bucket_tree_from_yaml",
]
