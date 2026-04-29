"""Validate a planner's proposal against user-placed keyframes.

Keyframes (§3.4.2 / §4.9) are the user's hard anchors in an otherwise
auto-planned WorkFlow. The planner may add nodes around them but must
honor three invariants:

1. **Locked trio preserved** — any keyframe with
   ``is_keyframe_locked=True`` must reappear in the plan with its
   exact trio (description / inputs / expected_outcome). Unlocked
   keyframes accept planner edits silently. (The schema has a
   ``keyframe_origin_trio`` slot for a future "show diff / restore
   original" UI feature, but neither writer nor reader exists today
   — see the field's docstring.)

2. **User-placed edges preserved** — every edge between two keyframes
   in the original WorkFlow must exist verbatim in the plan.
   "Preserved" means direction and endpoints identical — not merely
   reachability. A planner that inserts a node *on* the edge is
   dropping it, and that is rejected.

3. **Relative topological order preserved** — for any two keyframes
   ``A`` and ``B`` where ``A`` precedes ``B`` in the original's topo
   order (i.e. there is a directed path ``A → … → B``), ``A`` must
   still precede ``B`` in the plan's topo order.

A plan that violates any of these is rejected before its nodes are
materialized — better to fail loudly and bounce back to the user
than to silently drop their edits.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agentloom.engine.plan_output_parser import ParsedPlan, PlanNodeSpec
from agentloom.schemas.common import NodeId
from agentloom.schemas.workflow import WorkFlow, WorkFlowNode

ViolationKind = Literal[
    "locked_trio_edited",
    "keyframe_dropped",
    "edge_dropped",
    "topo_order_violated",
    "plan_has_cycle",
]


class KeyframeViolation(BaseModel):
    kind: ViolationKind
    message: str
    node_id: NodeId | None = None
    other_node_id: NodeId | None = None


class KeyframeValidationResult(BaseModel):
    violations: list[KeyframeViolation] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def _keyframes(wf: WorkFlow) -> dict[NodeId, WorkFlowNode]:
    return {nid: n for nid, n in wf.nodes.items() if n.is_keyframe}


def _plan_topo_index(plan: ParsedPlan) -> dict[NodeId, int] | None:
    """Return a map ``node_id -> position in topo order``, or None if
    the plan has a cycle (validator reports this separately)."""
    incoming: dict[NodeId, set[NodeId]] = {n.id: set(n.parent_ids) for n in plan.nodes}
    order: list[NodeId] = []
    ready = sorted(nid for nid, deps in incoming.items() if not deps)
    while ready:
        nid = ready.pop(0)
        order.append(nid)
        for other_id, deps in incoming.items():
            if nid in deps:
                deps.remove(nid)
                if not deps and other_id not in order and other_id not in ready:
                    ready.append(other_id)
        ready.sort()
    if len(order) != len(plan.nodes):
        return None
    return {nid: i for i, nid in enumerate(order)}


def _reachable(
    graph: dict[NodeId, list[NodeId]], source: NodeId, target: NodeId
) -> bool:
    """Is *target* reachable from *source* following directed edges?"""
    stack = [source]
    seen: set[NodeId] = set()
    while stack:
        nid = stack.pop()
        if nid == target:
            return True
        if nid in seen:
            continue
        seen.add(nid)
        stack.extend(graph.get(nid, ()))
    return False


def _child_map(wf: WorkFlow) -> dict[NodeId, list[NodeId]]:
    children: dict[NodeId, list[NodeId]] = {nid: [] for nid in wf.nodes}
    for nid, n in wf.nodes.items():
        for p in n.parent_ids:
            children.setdefault(p, []).append(nid)
    return children


def _trio_equal(original: WorkFlowNode, spec: PlanNodeSpec) -> tuple[bool, str]:
    """Compare the trio. Returns (equal, diff_summary)."""

    def eq_text(lhs_text: str, rhs: str) -> bool:
        return lhs_text == rhs

    def eq_opt_text(lhs_editable, rhs: str | None) -> bool:
        lhs_text = lhs_editable.text if lhs_editable is not None else None
        if lhs_text is None and (rhs is None or rhs == ""):
            return True
        return lhs_text == rhs

    diffs: list[str] = []
    if not eq_text(original.description.text, spec.description):
        diffs.append("description")
    if not eq_opt_text(original.inputs, spec.inputs):
        diffs.append("inputs")
    if not eq_opt_text(original.expected_outcome, spec.expected_outcome):
        diffs.append("expected_outcome")
    return not diffs, ",".join(diffs)


def validate_plan_against_keyframes(
    original: WorkFlow, plan: ParsedPlan
) -> KeyframeValidationResult:
    """Check *plan* honors every keyframe invariant from *original*."""
    result = KeyframeValidationResult()
    keyframes = _keyframes(original)

    topo_idx = _plan_topo_index(plan)
    if topo_idx is None:
        result.violations.append(
            KeyframeViolation(
                kind="plan_has_cycle",
                message="plan is not a DAG — node parent_ids form a cycle",
            )
        )
        # Continue validating what we can; topo checks will be skipped.

    # --- (1) locked trio + (k) presence ------------------------------
    for kid, kf in keyframes.items():
        spec = plan.get(kid)
        if spec is None:
            result.violations.append(
                KeyframeViolation(
                    kind="keyframe_dropped",
                    message=f"keyframe {kid} missing from plan",
                    node_id=kid,
                )
            )
            continue
        if kf.is_keyframe_locked:
            equal, diff = _trio_equal(kf, spec)
            if not equal:
                result.violations.append(
                    KeyframeViolation(
                        kind="locked_trio_edited",
                        message=(
                            f"locked keyframe {kid} has edited trio fields: {diff}"
                        ),
                        node_id=kid,
                    )
                )

    # --- (2) user-placed edges between keyframes ---------------------
    # For every original edge child.parent where both are keyframes,
    # the plan must carry parent in child.parent_ids exactly.
    for cid, child in original.nodes.items():
        if not child.is_keyframe:
            continue
        for pid in child.parent_ids:
            if pid not in keyframes:
                continue
            # both endpoints are keyframes; plan must keep the edge.
            spec = plan.get(cid)
            if spec is None:
                continue  # already reported as keyframe_dropped
            if pid not in spec.parent_ids:
                result.violations.append(
                    KeyframeViolation(
                        kind="edge_dropped",
                        message=(
                            f"user-placed edge {pid} -> {cid} "
                            f"missing in plan"
                        ),
                        node_id=pid,
                        other_node_id=cid,
                    )
                )

    # --- (3) relative topo order between keyframes -------------------
    if topo_idx is not None:
        orig_children = _child_map(original)
        kids = list(keyframes.keys())
        for i, a in enumerate(kids):
            for b in kids[i + 1 :]:
                # "a precedes b" in the original iff b is reachable from a.
                a_before_b = _reachable(orig_children, a, b)
                b_before_a = _reachable(orig_children, b, a)
                if not a_before_b and not b_before_a:
                    continue  # unordered in original, plan may pick either
                if a not in topo_idx or b not in topo_idx:
                    continue  # reported above as keyframe_dropped
                plan_a_before_b = topo_idx[a] < topo_idx[b]
                if a_before_b and not plan_a_before_b:
                    result.violations.append(
                        KeyframeViolation(
                            kind="topo_order_violated",
                            message=(
                                f"keyframe {a} precedes {b} in the original "
                                f"but not in the plan"
                            ),
                            node_id=a,
                            other_node_id=b,
                        )
                    )
                if b_before_a and plan_a_before_b:
                    result.violations.append(
                        KeyframeViolation(
                            kind="topo_order_violated",
                            message=(
                                f"keyframe {b} precedes {a} in the original "
                                f"but not in the plan"
                            ),
                            node_id=b,
                            other_node_id=a,
                        )
                    )

    return result
