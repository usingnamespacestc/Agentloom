/**
 * Custom node renderer for one WorkFlowNode (inner DAG).
 *
 * Three variants driven by ``step_kind``:
 * - ``llm_call`` — shows the model ref, the latest output text, and
 *   token usage (prompt / completion / cached)
 * - ``tool_call`` — shows tool name and a truncated tool_result body
 * - ``sub_agent_delegation`` — shows a stub label; full rendering
 *   lands with M10 once system workflows are implemented
 */

import { Handle, Position, type NodeProps } from "@xyflow/react";
import { useTranslation } from "react-i18next";

import { StatusBadge } from "./StatusBadge";
import type { WorkFlowNode } from "@/types/schema";

export interface WorkFlowNodeData extends Record<string, unknown> {
  node: WorkFlowNode;
  isSelected: boolean;
}

const KIND_ACCENT: Record<string, string> = {
  llm_call: "border-sky-300 bg-sky-50",
  tool_call: "border-emerald-300 bg-emerald-50",
  sub_agent_delegation: "border-violet-300 bg-violet-50",
};

function truncate(text: string, n = 140): string {
  if (text.length <= n) return text;
  return `${text.slice(0, n - 1)}…`;
}

export function WorkFlowNodeCard({ data }: NodeProps) {
  const { t } = useTranslation();
  const { node, isSelected } = data as WorkFlowNodeData;
  const accent = KIND_ACCENT[node.step_kind] ?? "border-gray-300 bg-white";

  return (
    <div
      data-testid={`workflow-node-${node.id}`}
      className={[
        "rounded-md border w-44 p-2 text-[11px] shadow-sm",
        accent,
        isSelected ? "ring-2 ring-blue-300" : "",
      ].join(" ")}
    >
      <Handle type="target" position={Position.Left} />

      <div className="flex items-center justify-between mb-1.5">
        <span className="font-semibold text-gray-700">
          {t(`node.kind.${node.step_kind}`)}
        </span>
        <StatusBadge status={node.status} />
      </div>

      {node.step_kind === "llm_call" && (
        <LlmCallBody node={node} />
      )}
      {node.step_kind === "tool_call" && (
        <ToolCallBody node={node} />
      )}
      {node.step_kind === "sub_agent_delegation" && (
        <div className="italic text-gray-500">delegation</div>
      )}

      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function LlmCallBody({ node }: { node: WorkFlowNode }) {
  const { t } = useTranslation();
  const output = node.output_message?.content ?? "";
  const usage = node.usage;
  const modelRef = node.model_override;

  return (
    <div className="space-y-1">
      {modelRef && (
        <div className="text-[10px] text-gray-500">
          {t("workflow.model")}: {modelRef.model_id}
        </div>
      )}
      <div className="text-gray-800 break-words whitespace-pre-wrap">
        {output ? truncate(output) : <span className="italic text-gray-400">—</span>}
      </div>
      {usage && (
        <div className="text-[10px] text-gray-500 flex gap-2">
          <span>{t("workflow.prompt_tokens")}: {usage.prompt_tokens}</span>
          <span>{t("workflow.completion_tokens")}: {usage.completion_tokens}</span>
          {usage.cached_tokens > 0 && (
            <span>{t("workflow.cached_tokens")}: {usage.cached_tokens}</span>
          )}
        </div>
      )}
    </div>
  );
}

function ToolCallBody({ node }: { node: WorkFlowNode }) {
  const { t } = useTranslation();
  const result = node.tool_result;
  const label = node.tool_name ?? "tool";
  return (
    <div className="space-y-1">
      <div className="font-mono text-gray-700">{label}</div>
      {result && (
        <div
          className={[
            "text-gray-800 break-words whitespace-pre-wrap",
            result.is_error ? "text-red-700" : "",
          ].join(" ")}
        >
          <span className="text-[10px] text-gray-500 mr-1">
            {result.is_error ? t("workflow.tool_error") : t("workflow.tool_result")}:
          </span>
          {truncate(result.content)}
        </div>
      )}
    </div>
  );
}
