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

import { useState } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import Markdown from "react-markdown";
import { useTranslation } from "react-i18next";

import { StatusBadge } from "./StatusBadge";
import { NodeIdLine } from "./NodeIdLine";
import { getRoleStyle } from "./roleStyles";
import { TokenBar } from "./ChatFlowNodeCard";
import { useChatFlowStore } from "@/store/chatflowStore";
import type { WorkFlowNode } from "@/types/schema";

export interface WorkFlowNodeData extends Record<string, unknown> {
  node: WorkFlowNode;
  isSelected: boolean;
  isRoot: boolean;
  isLeaf: boolean;
  maxContextTokens: number | null;
}

const KIND_ACCENT: Record<string, string> = {
  llm_call: "border-sky-300 bg-sky-50",
  tool_call: "border-emerald-300 bg-emerald-50",
  judge_call: "border-amber-300 bg-amber-50",
  sub_agent_delegation: "border-violet-300 bg-violet-50",
};

function truncate(text: string, n = 140): string {
  if (text.length <= n) return text;
  return `${text.slice(0, n - 1)}…`;
}

export function WorkFlowNodeCard({ data }: NodeProps) {
  const { t } = useTranslation();
  const { node, isSelected, isRoot, isLeaf, maxContextTokens } = data as WorkFlowNodeData;
  // Role-based styling takes precedence over step_kind — see roleStyles.ts.
  // Legacy (direct-mode) nodes have role === null and fall back to the
  // original step_kind accent so the MVP look is preserved.
  const roleStyle = getRoleStyle(node.role);
  const accent =
    roleStyle?.container ??
    KIND_ACCENT[node.step_kind] ??
    "border border-gray-300 bg-white";

  return (
    <div
      data-testid={`workflow-node-${node.id}`}
      data-role={node.role ?? "none"}
      className={[
        "rounded-md w-52 p-2 text-[11px] shadow-sm",
        // When there's no roleStyle, the legacy KIND_ACCENT string only
        // includes border-{hue}-300, so we need a default 1px border class.
        roleStyle ? "" : "border",
        accent,
        isSelected ? "ring-2 ring-blue-300" : "",
      ].join(" ")}
    >
      {!isRoot && <Handle type="target" position={Position.Left} />}

      <div className="flex items-center justify-between mb-1.5 gap-1">
        <span className="font-semibold text-gray-700">
          {t(`node.kind.${node.step_kind}`)}
        </span>
        {node.role && roleStyle && (
          <span
            data-testid={`role-badge-${node.role}`}
            className={`inline-flex items-center rounded px-1 py-0.5 text-[9px] font-medium leading-none ${roleStyle.badge}`}
          >
            {t(`node.role.${node.role}`)}
          </span>
        )}
        <StatusBadge status={node.status} />
      </div>

      {node.step_kind === "llm_call" && (
        <LlmCallBody node={node} maxCtx={maxContextTokens} />
      )}
      {node.step_kind === "tool_call" && (
        <ToolCallBody node={node} />
      )}
      {node.step_kind === "judge_call" && (
        <JudgeCallBody node={node} maxCtx={maxContextTokens} />
      )}
      {node.step_kind === "sub_agent_delegation" && (
        <SubAgentDelegationBody node={node} />
      )}

      <NodeIdLine nodeId={node.id} />

      {!isLeaf && <Handle type="source" position={Position.Right} />}
    </div>
  );
}

function LlmCallBody({ node, maxCtx }: { node: WorkFlowNode; maxCtx: number | null }) {
  const { t } = useTranslation();
  // While the node is RUNNING, show the live streaming buffer (one
  // SSE event per provider chunk). Once it terminates, the
  // server-refreshed ``output_message`` becomes authoritative and
  // the buffer is cleared by the store.
  const streamingDelta = useChatFlowStore(
    (s) => s.streamingDeltas[node.id] ?? "",
  );
  const output = node.output_message?.content ?? "";
  const live = node.status === "running" && streamingDelta;
  const thinking = node.output_message?.extras?.thinking;
  const usage = node.usage;
  const modelRef = node.model_override;

  return (
    <div className="space-y-1">
      {modelRef && (
        <div className="text-[10px] text-gray-500">
          {t("workflow.model")}: {modelRef.model_id}
        </div>
      )}
      {typeof thinking === "string" && thinking && (
        <ThinkingToggle text={thinking} label={t("conversation.thinking")} />
      )}
      <div className="prose prose-sm max-w-none text-[11px] text-gray-800 break-words">
        {live ? (
          <div data-testid="streaming-preview">
            <Markdown>{truncate(streamingDelta)}</Markdown>
            <span className="inline-block w-1 h-3 align-middle bg-sky-400 animate-pulse ml-0.5" />
          </div>
        ) : output ? (
          <Markdown>{truncate(output)}</Markdown>
        ) : (
          <span className="italic text-gray-400">—</span>
        )}
      </div>
      {usage && usage.total_tokens > 0 && (
        <TokenBar tokens={usage.total_tokens} maxTokens={maxCtx} />
      )}
    </div>
  );
}

function ThinkingToggle({ text, label }: { text: string; label: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1 text-[10px] text-gray-400 hover:text-gray-600"
      >
        <span className="inline-block transition-transform" style={{ transform: open ? "rotate(90deg)" : "rotate(0deg)" }}>
          ▸
        </span>
        {label}
      </button>
      {open && (
        <div className="mt-0.5 rounded border border-gray-100 bg-gray-50 px-1.5 py-1 text-[10px] text-gray-500 break-words max-h-32 overflow-auto">
          {truncate(text, 500)}
        </div>
      )}
    </div>
  );
}

function JudgeCallBody({ node, maxCtx }: { node: WorkFlowNode; maxCtx: number | null }) {
  const { t } = useTranslation();
  const variant = node.judge_variant;
  const verdict = node.judge_verdict;
  const streamingDelta = useChatFlowStore(
    (s) => s.streamingDeltas[node.id] ?? "",
  );
  const live = node.status === "running" && streamingDelta;

  // Pick the one-word headline that matches the variant's discriminator.
  const headline = verdict
    ? variant === "pre"
      ? verdict.feasibility
      : variant === "during"
        ? verdict.during_verdict
        : verdict.post_verdict
    : null;

  const headlineColor =
    headline === "ok" || headline === "accept" || headline === "continue"
      ? "text-green-700"
      : headline === "risky" || headline === "retry" || headline === "revise"
        ? "text-amber-700"
        : headline
          ? "text-red-700"
          : "text-gray-400";

  return (
    <div className="space-y-1">
      <div className="flex items-center gap-1 text-[10px] text-gray-500">
        <span className="rounded bg-amber-200/60 px-1 py-0.5 font-medium text-amber-900">
          {variant ? t(`workflow.judge_variant_${variant}`) : "—"}
        </span>
        {headline && (
          <span className={`font-semibold ${headlineColor}`}>{headline}</span>
        )}
      </div>
      {verdict?.blockers && verdict.blockers.length > 0 && (
        <BulletList label={t("workflow.blockers")} items={verdict.blockers} />
      )}
      {verdict?.missing_inputs && verdict.missing_inputs.length > 0 && (
        <BulletList label={t("workflow.missing_inputs")} items={verdict.missing_inputs} />
      )}
      {verdict?.critiques && verdict.critiques.length > 0 && (
        <BulletList
          label={t("workflow.critiques")}
          items={verdict.critiques.map((c) => `${c.severity}: ${c.issue}`)}
        />
      )}
      {verdict?.issues && verdict.issues.length > 0 && (
        <BulletList
          label={t("workflow.issues")}
          items={verdict.issues.map(
            (i) => `${i.location}: expected ${i.expected}, got ${i.actual}`,
          )}
        />
      )}
      {live && (
        <div
          data-testid="streaming-preview"
          className="prose prose-sm max-w-none text-[11px] text-gray-500 italic break-words"
        >
          <Markdown>{truncate(streamingDelta, 80)}</Markdown>
          <span className="inline-block w-1 h-3 align-middle bg-amber-500 animate-pulse ml-0.5" />
        </div>
      )}
      {!live && node.output_message?.content && !verdict && (
        <div className="prose prose-sm max-w-none text-[11px] text-gray-500 italic break-words">
          <Markdown>{truncate(node.output_message.content, 80)}</Markdown>
        </div>
      )}
      {node.usage && node.usage.total_tokens > 0 && (
        <TokenBar tokens={node.usage.total_tokens} maxTokens={maxCtx} />
      )}
    </div>
  );
}

function BulletList({ label, items }: { label: string; items: string[] }) {
  return (
    <div>
      <div className="text-[10px] font-medium text-gray-600">{label}</div>
      <ul className="ml-3 list-disc text-[10px] text-gray-700 space-y-0.5">
        {items.map((it, i) => (
          <li key={i} className="break-words">
            {truncate(it, 90)}
          </li>
        ))}
      </ul>
    </div>
  );
}

function SubAgentDelegationBody({ node }: { node: WorkFlowNode }) {
  const { t } = useTranslation();
  const enterSubWorkflow = useChatFlowStore((s) => s.enterSubWorkflow);
  const hasSub = node.sub_workflow != null;
  const childCount = hasSub ? Object.keys(node.sub_workflow!.nodes).length : 0;

  return (
    <div className="space-y-1">
      <div className="italic text-gray-500">delegation</div>
      {hasSub && (
        <button
          type="button"
          data-testid={`open-sub-workflow-${node.id}`}
          onClick={(e) => {
            e.stopPropagation();
            enterSubWorkflow(node.id);
          }}
          className="rounded border border-violet-300 bg-white px-1.5 py-0.5 text-[10px] text-violet-700 hover:bg-violet-100"
        >
          {t("workflow.open_sub_workflow")} ({childCount})
        </button>
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
        <div>
          <span className="text-[10px] text-gray-500 mr-1">
            {result.is_error ? t("workflow.tool_error") : t("workflow.tool_result")}:
          </span>
          <div
            className={[
              "prose prose-sm max-w-none text-[11px] break-words",
              result.is_error ? "text-red-700" : "text-gray-800",
            ].join(" ")}
          >
            <Markdown>{truncate(result.content)}</Markdown>
          </div>
        </div>
      )}
    </div>
  );
}
