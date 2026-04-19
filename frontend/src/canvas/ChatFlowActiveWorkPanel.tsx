/**
 * Bottom-right floating panel on the ChatFlow canvas listing WorkNodes
 * that are *actually* running right now, across all ChatNodes'
 * workflows and any nested sub_workflows.
 *
 * Filter:
 *   - status === "running" — the engine only flips a node to RUNNING
 *     once ``_run_node`` actually starts executing it, so this already
 *     excludes queued-but-not-started nodes.
 *   - sub_agent_delegation containers are skipped; their children (the
 *     real workers) surface here instead.
 *
 * All other step_kinds surface — judge_call (structured output, no
 * streaming) and planner llm_call (tool-use, no streaming) both need
 * visibility while they're in flight.
 *
 * Rows show step_kind, short id, and the first line of description.
 * Clicking a row drills the canvas into that WorkNode (handles any
 * sub_workflow nesting via jumpToWorkNode).
 */

import { useState } from "react";
import { useTranslation } from "react-i18next";

import { useChatFlowStore } from "@/store/chatflowStore";
import type { ChatFlow, NodeId, StepKind, WorkFlow, WorkFlowNode } from "@/types/schema";

export interface ChatFlowActiveWorkPanelProps {
  chatflow: ChatFlow | null;
}

interface ActiveEntry {
  chatNodeId: NodeId;
  subPath: NodeId[];
  node: WorkFlowNode;
  depth: number;
}

const STEP_KIND_COLOR: Record<StepKind, string> = {
  llm_call: "bg-blue-100 text-blue-800",
  tool_call: "bg-amber-100 text-amber-800",
  judge_call: "bg-purple-100 text-purple-800",
  sub_agent_delegation: "bg-gray-100 text-gray-700",
  compact: "bg-teal-100 text-teal-800",
};

function collectActiveWorkNodes(
  chat: ChatFlow | null,
): ActiveEntry[] {
  if (!chat) return [];
  const out: ActiveEntry[] = [];

  function walk(
    wf: WorkFlow,
    chatNodeId: NodeId,
    subPath: NodeId[],
    depth: number,
  ): void {
    for (const node of Object.values(wf.nodes)) {
      const wn = node as WorkFlowNode;
      if (wn.sub_workflow) {
        // Container — recurse; do NOT emit a row for the delegation itself.
        walk(wn.sub_workflow, chatNodeId, [...subPath, wn.id], depth + 1);
        continue;
      }
      if (wn.status !== "running") continue;
      out.push({ chatNodeId, subPath, node: wn, depth });
    }
  }

  for (const chatNode of Object.values(chat.nodes)) {
    if (chatNode.status !== "running") continue;
    if (!chatNode.workflow) continue;
    walk(chatNode.workflow, chatNode.id, [], 0);
  }
  return out;
}

function descriptionOneLine(node: WorkFlowNode): string {
  const text = node.description?.text?.trim() ?? "";
  if (!text) return "";
  const firstLine = text.split(/\r?\n/, 1)[0] ?? "";
  return firstLine;
}

export function ChatFlowActiveWorkPanel({ chatflow }: ChatFlowActiveWorkPanelProps) {
  const { t } = useTranslation();
  const jumpToWorkNode = useChatFlowStore((s) => s.jumpToWorkNode);
  const [open, setOpen] = useState(true);

  const active = collectActiveWorkNodes(chatflow);
  const count = active.length;

  return (
    <div
      data-testid="chatflow-active-work-panel"
      className="absolute bottom-2 right-2 z-10 w-72 rounded-md border border-gray-300 bg-white/95 shadow-sm"
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-2 rounded-md px-2 py-1 text-[11px] text-gray-700 hover:bg-gray-50"
      >
        <span className="font-medium">
          {t("chatflow.active_work")} ({count})
        </span>
        <span className="text-gray-400">{open ? "▾" : "▸"}</span>
      </button>
      {open && count > 0 && (
        <ul className="max-h-64 overflow-auto border-t border-gray-200 text-[11px]">
          {active.map((entry, i) => {
            const desc = descriptionOneLine(entry.node);
            return (
              <li
                key={`${entry.chatNodeId}:${entry.subPath.join("/")}:${entry.node.id}:${i}`}
                onClick={() =>
                  jumpToWorkNode(entry.chatNodeId, entry.subPath, entry.node.id)
                }
                className="cursor-pointer border-b border-gray-100 px-2 py-1 last:border-b-0 hover:bg-gray-50"
                title={entry.node.description?.text ?? ""}
              >
                <div className="flex items-center gap-1 text-[10px]">
                  {entry.depth > 0 && (
                    <span className="font-mono text-gray-400">
                      {"›".repeat(entry.depth)}
                    </span>
                  )}
                  <span
                    className={`rounded px-1 py-[1px] font-mono ${STEP_KIND_COLOR[entry.node.step_kind]}`}
                  >
                    {entry.node.step_kind}
                  </span>
                  <span className="font-mono text-gray-500">
                    {entry.node.id.slice(-6)}
                  </span>
                  {entry.node.role && (
                    <span className="text-gray-500">· {entry.node.role}</span>
                  )}
                </div>
                {desc && (
                  <div className="mt-0.5 break-words leading-snug text-gray-700">
                    {desc}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {open && count === 0 && (
        <div className="border-t border-gray-200 px-2 py-1 text-[11px] italic text-gray-400">
          {t("chatflow.active_work_empty")}
        </div>
      )}
    </div>
  );
}
