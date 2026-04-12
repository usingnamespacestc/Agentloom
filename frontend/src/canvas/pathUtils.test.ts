/**
 * Unit tests for the Conversation path resolver.
 *
 * The resolver's contract (M8.5 round 2):
 * - path ends strictly at the selected node (no extension past it)
 * - forks are emitted for any path node with >1 children
 * - chosenChildId is the next node in the path, or null if the path
 *   terminates at that fork
 * - no selection falls back to the default "latest child" walk
 *
 * Branch memory (switching back to a previously visited endpoint) is
 * a store concept, not a resolver concept, so it's tested in
 * chatflowStore.test.ts.
 */

import { describe, it, expect } from "vitest";

import { resolvePath, findLatestLeafId } from "./pathUtils";
import type { NodeBaseFields } from "@/types/schema";

function n(
  id: string,
  parents: string[] = [],
  created: string = `2026-04-10T00:00:${id.padStart(2, "0")}Z`,
): NodeBaseFields {
  return {
    id,
    parent_ids: parents,
    description: { text: "", provenance: "unset", updated_at: created },
    expected_outcome: null,
    status: "succeeded",
    model_override: null,
    locked: false,
    error: null,
    created_at: created,
    updated_at: created,
    started_at: null,
    finished_at: null,
  };
}

describe("resolvePath", () => {
  it("returns empty result for null or empty graphs", () => {
    expect(resolvePath(null, null)).toEqual({ path: [], forks: [] });
    expect(resolvePath({ nodes: {}, rootIds: [] }, null)).toEqual({
      path: [],
      forks: [],
    });
  });

  it("walks a linear chain from root to leaf when nothing is selected", () => {
    const graph = {
      nodes: {
        a: n("a"),
        b: n("b", ["a"]),
        c: n("c", ["b"]),
      },
      rootIds: ["a"],
    };
    const { path, forks } = resolvePath(graph, null);
    expect(path).toEqual(["a", "b", "c"]);
    expect(forks).toEqual([]);
  });

  it("picks the latest child by default at a fork when nothing is selected", () => {
    const graph = {
      nodes: {
        a: n("a"),
        b: n("b", ["a"], "2026-04-10T00:00:01Z"),
        c: n("c", ["a"], "2026-04-10T00:00:02Z"),
      },
      rootIds: ["a"],
    };
    const { path, forks } = resolvePath(graph, null);
    expect(path).toEqual(["a", "c"]);
    expect(forks).toHaveLength(1);
    expect(forks[0].nodeId).toBe("a");
    expect(forks[0].chosenChildId).toBe("c");
    expect(forks[0].childIds).toEqual(["b", "c"]);
  });

  it("terminates strictly at the selected node — no extension past it", () => {
    const graph = {
      nodes: {
        a: n("a"),
        b: n("b", ["a"]),
        c: n("c", ["b"]),
      },
      rootIds: ["a"],
    };
    // Selecting a must not extend the path forward to b or c.
    expect(resolvePath(graph, "a").path).toEqual(["a"]);
    // Selecting b must not extend to c.
    expect(resolvePath(graph, "b").path).toEqual(["a", "b"]);
    // Selecting c is the full chain.
    expect(resolvePath(graph, "c").path).toEqual(["a", "b", "c"]);
  });

  it("routes through the selected node at a fork", () => {
    const graph = {
      nodes: {
        a: n("a"),
        b: n("b", ["a"], "2026-04-10T00:00:01Z"),
        c: n("c", ["a"], "2026-04-10T00:00:02Z"),
      },
      rootIds: ["a"],
    };
    // Default would pick c; selecting b must force the path through b
    // instead, and the fork entry must mark b as chosen.
    const { path, forks } = resolvePath(graph, "b");
    expect(path).toEqual(["a", "b"]);
    expect(forks).toHaveLength(1);
    expect(forks[0].chosenChildId).toBe("b");
  });

  it("emits a fork with chosenChildId=null when the path ends at a fork", () => {
    const graph = {
      nodes: {
        a: n("a"),
        b: n("b", ["a"], "2026-04-10T00:00:01Z"),
        c: n("c", ["a"], "2026-04-10T00:00:02Z"),
      },
      rootIds: ["a"],
    };
    const { path, forks } = resolvePath(graph, "a");
    expect(path).toEqual(["a"]);
    expect(forks).toHaveLength(1);
    expect(forks[0].chosenChildId).toBeNull();
    expect(forks[0].childIds).toEqual(["b", "c"]);
  });

  it("findLatestLeafId returns the default-walk leaf", () => {
    const graph = {
      nodes: {
        a: n("a"),
        b: n("b", ["a"], "2026-04-10T00:00:01Z"),
        c: n("c", ["a"], "2026-04-10T00:00:02Z"),
      },
      rootIds: ["a"],
    };
    expect(findLatestLeafId(graph)).toBe("c");
  });

  it("returns empty for a graph with a ghost root", () => {
    const graph = { nodes: {}, rootIds: ["ghost"] };
    expect(resolvePath(graph, null)).toEqual({ path: [], forks: [] });
  });
});
