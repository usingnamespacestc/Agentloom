import { useState } from "react";
import { useTranslation } from "react-i18next";

import { usePreferencesStore } from "@/store/preferencesStore";

export function NodeIdLine({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation();
  const showNodeId = usePreferencesStore((s) => s.showNodeId);
  const [copied, setCopied] = useState(false);

  if (!showNodeId) return null;

  const onClick = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(nodeId);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 900);
    } catch {
      // clipboard API unavailable — ignore
    }
  };

  return (
    <div
      onClick={onClick}
      className="mt-1 cursor-pointer truncate font-mono text-[9px] text-gray-400 hover:text-blue-500"
      title={copied ? t("common.copied") : nodeId}
    >
      {copied ? t("common.copied") : nodeId}
    </div>
  );
}
