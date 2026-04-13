"""Resolve the effective model for a WorkNode.

ChatFlow-level model inheritance used to live here too (walking
``next_model_override`` up the WorkFlow DAG). That was removed when
model selection moved from per-node overrides to a per-turn choice in
the ChatFlow composer (§4.10 rework):

- The ChatFlow composer captures the user's pick and the engine
  snapshots it onto the new ChatNode's ``resolved_model``.
- At spawn time the engine stamps each WorkFlowNode's
  ``model_override`` with that resolved value, and propagates it across
  retries / tool-call follow-ups.

So by the time this resolver runs, every WorkNode has its
``model_override`` set. This function is kept as a thin accessor in
case we later need per-WorkNode overrides for the per-call-type split
(see memory ``project_agentloom_per_call_type_models``).
"""

from __future__ import annotations

from agentloom.schemas import WorkFlow
from agentloom.schemas.common import NodeId, ProviderModelRef


def effective_model_for(
    workflow: WorkFlow,
    node_id: NodeId,
) -> ProviderModelRef | None:
    """Return the :class:`ProviderModelRef` that should be used when
    invoking *node_id* in *workflow*.

    Resolution today is just ``node.model_override``. Returning ``None``
    tells the caller to fall back to the workspace / provider default.

    Raises :class:`KeyError` if *node_id* is not in *workflow*.
    """
    return workflow.get(node_id).model_override
