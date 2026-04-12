/**
 * Path resolution for the Conversation view.
 *
 * Given a DAG (ChatFlow or inner WorkFlow) and the currently selected
 * node id, compute the chain of node ids from the root to (and strictly
 * ending at) that selected node. The Conversation view renders messages
 * in that chain — nothing past the selected node is shown, because the
 * user wants the right panel to reflect the exact prefix they clicked.
 *
 * Walking rules (M8.5 round 2):
 * 1. If ``selectedId`` is non-null and known, the endpoint is it.
 *    Otherwise the endpoint is the default latest leaf (walk from the
 *    first root, always taking the latest child).
 * 2. From the endpoint, walk ancestors via ``parent_ids[0]`` up to a
 *    root and reverse. For a pure tree this is exact; for a DAG with
 *    diamonds it picks one specific incoming path (good enough for
 *    M8.5 — M9 may add full branch-specific ancestor resolution).
 * 3. For every node on the path that has >1 children, emit a
 *    ``ForkInfo`` entry. ``chosenChildId`` is the next node in the
 *    path if one exists, or ``null`` when the path terminates at that
 *    fork (i.e. the user selected the fork itself — no branch yet).
 *
 * The function is pure and generic so the same helper works for both
 * the outer ChatFlow path in the Conversation view and the inner
 * WorkFlow path in the drill-down view.
 *
 * Branch *memory* (the "switch back to a previously visited endpoint")
 * is NOT handled here — it lives in the store, because remembering a
 * branch endpoint is a UI/navigation concept, not a graph property.
 */

import type { NodeBaseFields, NodeId } from "@/types/schema";

export interface ForkInfo {
  /** The fork node id — its children are the branches. */
  nodeId: NodeId;
  /** All child ids in stable order (created_at ascending, then id). */
  childIds: NodeId[];
  /** The child the path currently takes, or null if the path ends here. */
  chosenChildId: NodeId | null;
}

export interface ResolvedPath {
  /** Ordered node ids from root to the endpoint (inclusive both ends). */
  path: NodeId[];
  /** One entry per fork encountered along the path, in path order. */
  forks: ForkInfo[];
}

export interface PathGraph<T extends NodeBaseFields> {
  nodes: Record<NodeId, T>;
  rootIds: NodeId[];
}

export function resolvePath<T extends NodeBaseFields>(
  graph: PathGraph<T> | null,
  selectedId: NodeId | null,
): ResolvedPath {
  if (!graph || graph.rootIds.length === 0) {
    return { path: [], forks: [] };
  }

  const childrenOf: Record<NodeId, NodeId[]> = {};
  for (const id of Object.keys(graph.nodes)) childrenOf[id] = [];
  for (const [id, node] of Object.entries(graph.nodes)) {
    for (const p of node.parent_ids) {
      if (p in childrenOf) childrenOf[p].push(id);
    }
  }
  for (const arr of Object.values(childrenOf)) {
    arr.sort((a, b) => compareForStability(graph.nodes[a], graph.nodes[b]));
  }

  // Endpoint: the selected node, or the default latest leaf if nothing
  // is selected (or the selection is stale/unknown).
  let endpoint: NodeId | null = null;
  if (selectedId && selectedId in graph.nodes) {
    endpoint = selectedId;
  } else {
    endpoint = defaultLatestLeaf(graph, childrenOf);
  }
  if (endpoint === null) return { path: [], forks: [] };

  // Walk ancestors from the endpoint back to a root (first-parent).
  const path: NodeId[] = [];
  const guard = new Set<NodeId>();
  let cursor: NodeId | null = endpoint;
  while (cursor !== null && !guard.has(cursor)) {
    guard.add(cursor);
    path.unshift(cursor);
    const parents: NodeId[] = graph.nodes[cursor]?.parent_ids ?? [];
    cursor = parents.length > 0 && parents[0] in graph.nodes ? parents[0] : null;
  }

  // Emit a fork entry for every node on the path that has >1 children.
  // chosenChildId = the next node in the path, or null if path ends here.
  const forks: ForkInfo[] = [];
  for (let i = 0; i < path.length; i++) {
    const nid = path[i];
    const children = childrenOf[nid] ?? [];
    if (children.length > 1) {
      const chosen: NodeId | null = i + 1 < path.length ? path[i + 1] : null;
      forks.push({ nodeId: nid, childIds: children, chosenChildId: chosen });
    }
  }

  return { path, forks };
}

/**
 * Return the default-walk leaf id (no selection, always-latest-child).
 * Used to auto-select something on first load so the Conversation view
 * has content without the user clicking.
 */
export function findLatestLeafId<T extends NodeBaseFields>(
  graph: PathGraph<T> | null,
): NodeId | null {
  if (!graph || graph.rootIds.length === 0) return null;
  const childrenOf: Record<NodeId, NodeId[]> = {};
  for (const id of Object.keys(graph.nodes)) childrenOf[id] = [];
  for (const [id, node] of Object.entries(graph.nodes)) {
    for (const p of node.parent_ids) {
      if (p in childrenOf) childrenOf[p].push(id);
    }
  }
  for (const arr of Object.values(childrenOf)) {
    arr.sort((a, b) => compareForStability(graph.nodes[a], graph.nodes[b]));
  }
  return defaultLatestLeaf(graph, childrenOf);
}

function defaultLatestLeaf<T extends NodeBaseFields>(
  graph: PathGraph<T>,
  childrenOf: Record<NodeId, NodeId[]>,
): NodeId | null {
  const root = graph.rootIds[0];
  if (!root || !(root in graph.nodes)) return null;
  const visited = new Set<NodeId>();
  let cursor: NodeId = root;
  while (!visited.has(cursor)) {
    visited.add(cursor);
    const children = childrenOf[cursor] ?? [];
    if (children.length === 0) return cursor;
    cursor = children[children.length - 1];
    if (!(cursor in graph.nodes)) return null;
  }
  return cursor;
}

function compareForStability(a: NodeBaseFields, b: NodeBaseFields): number {
  if (a.created_at !== b.created_at) return a.created_at < b.created_at ? -1 : 1;
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}
