/**
 * Shared markdown renderer with GFM tables + inline HTML pass-through.
 *
 * Why a wrapper instead of bare `<Markdown>` calls scattered across
 * ConversationView / ChatFlowNodeCard / WorkFlowNodeCard:
 * - `react-markdown` v10 defaults to CommonMark only — no GFM tables,
 *   no `<br>` rendering. Models love emitting `| col | col |` with
 *   `<br>`-separated multi-line cells; out of the box those rendered
 *   as plain text. (User-facing report 2026-04-26.)
 * - Need exactly one place to wire plugins so every surface picks up
 *   the same config (and any future config changes — e.g. enabling
 *   math, syntax highlight, footnotes — happen once).
 *
 * Plugin choices:
 * - `remark-gfm`: GFM tables, strikethrough, task lists, autolinks.
 * - `rehype-raw`: parse raw HTML inside markdown so `<br>` actually
 *   line-breaks. Required for the multi-line table cells the LLM
 *   tends to emit.
 * - `rehype-sanitize`: white-list HTML elements so `rehype-raw` can't
 *   leak XSS via untrusted LLM output. Defaults block `<script>`,
 *   `<iframe>`, event handlers, etc. — keep `<br>`, `<sup>`, `<sub>`,
 *   inline formatting.
 *
 * Plugin order matters: rehypeRaw → rehypeSanitize. Sanitize must run
 * AFTER raw HTML enters the tree, otherwise it can't see the nodes
 * to scrub.
 */
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import Markdown, { type Components } from "react-markdown";

const sanitizeSchema = {
  ...defaultSchema,
  // defaults block `<br>` content via the schema's tagNames whitelist —
  // but `defaultSchema.tagNames` already includes `br`. Augment with
  // common inline HTML the LLM emits.
  tagNames: [
    ...(defaultSchema.tagNames || []),
    "details",
    "summary",
    "sub",
    "sup",
    "mark",
  ],
};

interface Props {
  children: string;
  /**
   * Optional component overrides. Forwarded to react-markdown — useful
   * when a specific surface wants to constrain link targets, restyle
   * code blocks, etc. Most callers shouldn't need this.
   */
  components?: Components;
  /** Apply tighter line-height / smaller code blocks via wrapper class. */
  className?: string;
}

export default function MarkdownView({
  children,
  components,
  className,
}: Props) {
  return (
    <div className={className}>
      <Markdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeRaw, [rehypeSanitize, sanitizeSchema]]}
        components={components}
      >
        {children}
      </Markdown>
    </div>
  );
}
