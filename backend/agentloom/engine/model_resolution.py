"""Resolve the effective model for a WorkNode by walking the
``next_model_override`` chain (ADR-022, §4.10 of requirements).

Semantics
---------
- A node's own ``model_override`` is an explicit pin for *that node
  only* — it does not propagate to descendants.
- A node's ``next_model_override`` is the pointer descendants inherit
  unless they (or an intermediate ancestor) sets their own.

The resolver walks ancestors in reverse topological order and returns
the first non-null value it finds. If none is set, the caller should
fall back to ``ChatFlow.default_model`` (out of scope here — this
function only sees one WorkFlow).

This lives next to the engine (not in ``schemas/``) because it's purely
runtime resolution logic — the schema is an inert carrier of fields.
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

    Resolution order:

    1. ``node.model_override`` — explicit pin on the node itself.
    2. Nearest ancestor with ``next_model_override`` set (walking
       parents; if a node has multiple parents the first non-null
       wins, with deterministic traversal via
       :meth:`WorkFlow.ancestors`).
    3. ``None`` — caller falls back to ChatFlow / workspace default.

    Raises :class:`KeyError` if *node_id* is not in *workflow*.
    """
    node = workflow.get(node_id)
    if node.model_override is not None:
        return node.model_override

    # ``workflow.ancestors`` returns topological order with root first,
    # so reversing gives nearest-ancestor-first.
    for aid in reversed(workflow.ancestors(node_id)):
        ancestor = workflow.get(aid)
        if ancestor.next_model_override is not None:
            return ancestor.next_model_override
    return None
