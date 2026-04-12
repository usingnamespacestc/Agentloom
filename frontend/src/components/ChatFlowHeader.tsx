/**
 * Top header bar — shows the current chatflow's title, description, and
 * tags, all inline-editable. Replaces the old static app title.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { ProviderSettings } from "@/components/ProviderSettings";
import { useChatFlowStore } from "@/store/chatflowStore";

export function ChatFlowHeader() {
  const { t, i18n } = useTranslation();
  const chatflow = useChatFlowStore((s) => s.chatflow);
  const patchChatFlow = useChatFlowStore((s) => s.patchChatFlow);
  const [settingsOpen, setSettingsOpen] = useState(false);

  if (!chatflow) {
    return (
      <header className="flex items-center justify-between border-b border-gray-200 bg-white px-4 py-2">
        <span className="text-sm text-gray-400">{t("app.no_chatflow")}</span>
        <div className="flex items-center gap-2">
          <SettingsButton onClick={() => setSettingsOpen(true)} />
          <LanguageToggle i18n={i18n} t={t} />
        </div>
        <ProviderSettings open={settingsOpen} onClose={() => setSettingsOpen(false)} />
      </header>
    );
  }

  return (
    <header className="flex items-center gap-3 border-b border-gray-200 bg-white px-4 py-1.5">
      {/* Left: title + description + tags */}
      <div className="min-w-0 flex-1">
        <EditableTitle
          value={chatflow.title ?? ""}
          placeholder={t("app.untitled")}
          onCommit={(v) => void patchChatFlow({ title: v || null })}
        />
        <EditableDescription
          value={chatflow.description ?? ""}
          placeholder={t("app.add_description")}
          onCommit={(v) => void patchChatFlow({ description: v || null })}
        />
        <TagList
          tags={chatflow.tags ?? []}
          placeholder={t("app.add_tag")}
          onChange={(tags) => void patchChatFlow({ tags })}
        />
      </div>

      {/* Right: settings + language toggle */}
      <div className="flex items-center gap-2">
        <SettingsButton onClick={() => setSettingsOpen(true)} />
        <LanguageToggle i18n={i18n} t={t} />
      </div>
      <ProviderSettings open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </header>
  );
}

// ---------------------------------------------------------------- Title

function EditableTitle({
  value,
  placeholder,
  onCommit,
}: {
  value: string;
  placeholder: string;
  onCommit: (v: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setDraft(value);
  }, [value]);

  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  const commit = () => {
    setEditing(false);
    const trimmed = draft.trim();
    if (trimmed !== value) onCommit(trimmed);
  };

  if (!editing) {
    return (
      <h1
        onClick={() => setEditing(true)}
        className="cursor-pointer truncate text-base font-semibold text-gray-900 hover:text-blue-600"
        title={value || placeholder}
      >
        {value || <span className="font-normal text-gray-400">{placeholder}</span>}
      </h1>
    );
  }

  return (
    <input
      ref={inputRef}
      type="text"
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit();
        if (e.key === "Escape") {
          setDraft(value);
          setEditing(false);
        }
      }}
      placeholder={placeholder}
      className="w-full border-b border-blue-400 bg-transparent text-base font-semibold text-gray-900 outline-none"
    />
  );
}

// ---------------------------------------------------------------- Description

function EditableDescription({
  value,
  placeholder,
  onCommit,
}: {
  value: string;
  placeholder: string;
  onCommit: (v: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setDraft(value);
  }, [value]);

  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  const commit = () => {
    setEditing(false);
    const trimmed = draft.trim();
    if (trimmed !== value) onCommit(trimmed);
  };

  if (!editing) {
    return (
      <p
        onClick={() => setEditing(true)}
        className="cursor-pointer truncate text-xs text-gray-500 hover:text-blue-500"
      >
        {value || <span className="italic text-gray-400">{placeholder}</span>}
      </p>
    );
  }

  return (
    <input
      ref={inputRef}
      type="text"
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit();
        if (e.key === "Escape") {
          setDraft(value);
          setEditing(false);
        }
      }}
      placeholder={placeholder}
      className="w-full border-b border-blue-400 bg-transparent text-xs text-gray-500 outline-none"
    />
  );
}

// ---------------------------------------------------------------- Tags

function TagList({
  tags,
  placeholder,
  onChange,
}: {
  tags: string[];
  placeholder: string;
  onChange: (tags: string[]) => void;
}) {
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (adding) inputRef.current?.focus();
  }, [adding]);

  const addTag = () => {
    const trimmed = draft.trim();
    setAdding(false);
    setDraft("");
    if (trimmed && !tags.includes(trimmed)) {
      onChange([...tags, trimmed]);
    }
  };

  const removeTag = useCallback(
    (tag: string) => {
      onChange(tags.filter((t) => t !== tag));
    },
    [tags, onChange],
  );

  return (
    <div className="mt-0.5 flex flex-wrap items-center gap-1">
      {tags.map((tag) => (
        <span
          key={tag}
          className="group/tag inline-flex items-center gap-0.5 rounded-full bg-gray-100 px-2 py-0.5 text-[10px] text-gray-600"
        >
          {tag}
          <button
            type="button"
            onClick={() => removeTag(tag)}
            className="hidden h-3 w-3 items-center justify-center rounded-full text-[8px] text-gray-400 hover:bg-gray-300 hover:text-gray-700 group-hover/tag:inline-flex"
          >
            {"\u2715"}
          </button>
        </span>
      ))}
      {adding ? (
        <input
          ref={inputRef}
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={addTag}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              addTag();
            }
            if (e.key === "Escape") {
              setAdding(false);
              setDraft("");
            }
          }}
          className="w-20 border-b border-blue-400 bg-transparent text-[10px] text-gray-600 outline-none"
          placeholder={placeholder}
        />
      ) : (
        <button
          type="button"
          onClick={() => setAdding(true)}
          className="rounded-full border border-dashed border-gray-300 px-1.5 py-0.5 text-[10px] text-gray-400 hover:border-blue-400 hover:text-blue-500"
        >
          + {placeholder}
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- Settings button

function SettingsButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded border border-gray-300 bg-white text-sm text-gray-500 hover:bg-gray-50 hover:text-gray-700"
      title="Settings"
    >
      {"\u2699"}
    </button>
  );
}

// ---------------------------------------------------------------- Language

function LanguageToggle({
  i18n,
  t,
}: {
  i18n: { changeLanguage: (l: string) => void; language: string };
  t: (key: string) => string;
}) {
  return (
    <button
      type="button"
      className="flex-shrink-0 rounded border border-gray-300 bg-white px-3 py-1 text-xs text-gray-700 hover:bg-gray-50"
      onClick={() =>
        i18n.changeLanguage(i18n.language === "zh-CN" ? "en-US" : "zh-CN")
      }
    >
      {t("app.switch_language")}
    </button>
  );
}
