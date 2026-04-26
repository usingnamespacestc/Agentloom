# Copyright Sierra
#
# AGENTLOOM VENDOR PATCH (2026-04-26): we drop the ``Agent`` re-export
# because we don't vendor ``tau_bench/agents/`` (Agentloom backend never
# uses upstream agent loops — runner does, in its own conda env). The
# original line was:
#     from tau_bench.agents.base import Agent as Agent
# See backend/vendor/tau_bench/PATCHES.md for the full vendor patch list.

from tau_bench.envs.base import Env as Env
