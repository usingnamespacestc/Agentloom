/**
 * Unit tests for the DAG layout helper.
 *
 * These stay focused on the graph shape (levels, stable ordering)
 * rather than pixel math, because the pixel math is a trivial
 * multiplication that'll catch itself via visual review.
 */

import { describe, it, expect } from "vitest";

import { layoutDag } from "./layout";
import type { NodeBaseFields } from "@/types/schema";

function makeNode(
  id: string,
  parents: string[],
  created: string = "2026-04-10T00:00:00Z",
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
    position_x: null,
    position_y: null,
    created_at: created,
    updated_at: created,
    started_at: null,
    finished_at: null,
  };
}

describe("layoutDag", () => {
  it("assigns root nodes to level 0", () => {
    const nodes = { a: makeNode("a", []), b: makeNode("b", []) };
    const result = layoutDag(nodes, ["a", "b"]);
    expect(result.every((r) => r.level === 0)).toBe(true);
  });

  it("assigns children to parent level + 1", () => {
    const nodes = {
      a: makeNode("a", []),
      b: makeNode("b", ["a"]),
      c: makeNode("c", ["b"]),
    };
    const result = layoutDag(nodes, ["a"]);
    const byId = Object.fromEntries(result.map((r) => [r.node.id, r]));
    expect(byId.a.level).toBe(0);
    expect(byId.b.level).toBe(1);
    expect(byId.c.level).toBe(2);
  });

  it("lifts merge nodes to max(parent) + 1", () => {
    const nodes = {
      a: makeNode("a", []),
      b: makeNode("b", []),
      c: makeNode("c", ["a"]),
      m: makeNode("m", ["b", "c"]),
    };
    const result = layoutDag(nodes, ["a", "b"]);
    const byId = Object.fromEntries(result.map((r) => [r.node.id, r]));
    // b is level 0, c is level 1, so merge lifts to level 2.
    expect(byId.m.level).toBe(2);
  });

  it("orders siblings by creation time", () => {
    const nodes = {
      a: makeNode("a", [], "2026-04-10T00:00:01Z"),
      b: makeNode("b", [], "2026-04-10T00:00:00Z"),
    };
    const result = layoutDag(nodes, ["a", "b"]);
    // b is older so it should come first in the level.
    const levelZero = result.filter((r) => r.level === 0);
    expect(levelZero.map((r) => r.node.id)).toEqual(["b", "a"]);
  });

  it("produces pixel positions for horizontal layout (levels along x)", () => {
    const nodes = {
      a: makeNode("a", []),
      b: makeNode("b", ["a"]),
    };
    const result = layoutDag(nodes, ["a"], {
      columnWidth: 100,
      rowHeight: 50,
      offsetX: 0,
      offsetY: 0,
      direction: "horizontal",
    });
    const byId = Object.fromEntries(result.map((r) => [r.node.id, r]));
    expect(byId.a.position).toEqual({ x: 0, y: 0 });
    // Level 1 → x advances by columnWidth; sibling index 0 → y stays at 0.
    expect(byId.b.position).toEqual({ x: 100, y: 0 });
  });

  it("produces pixel positions for vertical layout (levels along y)", () => {
    const nodes = {
      a: makeNode("a", []),
      b: makeNode("b", ["a"]),
    };
    const result = layoutDag(nodes, ["a"], {
      columnWidth: 100,
      rowHeight: 50,
      offsetX: 0,
      offsetY: 0,
      direction: "vertical",
    });
    const byId = Object.fromEntries(result.map((r) => [r.node.id, r]));
    expect(byId.a.position).toEqual({ x: 0, y: 0 });
    expect(byId.b.position).toEqual({ x: 0, y: 50 });
  });

  it("handles dangling parent refs without crashing", () => {
    const nodes = { a: makeNode("a", ["ghost"]) };
    const result = layoutDag(nodes, []);
    // Unknown parents are ignored and the node gets level 0.
    expect(result).toHaveLength(1);
    expect(result[0].level).toBe(0);
  });
});
