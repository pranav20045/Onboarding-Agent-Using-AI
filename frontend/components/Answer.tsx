"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { CITATION_RE, parseCitation, type AnswerResult, type Citation } from "@/lib/api";

const CITE_PREFIX = "#cite=";

/** Turn `[path:1-2]` citations into markdown links that render as clickable chips. */
function linkCitations(text: string): string {
  return text.replace(CITATION_RE, (raw) => `[${raw.slice(1, -1)}](${CITE_PREFIX}${encodeURIComponent(raw)})`);
}

export function AnswerMarkdown({
  text,
  onCite,
  testId = "answer",
}: {
  text: string;
  onCite: (c: Citation) => void;
  testId?: string;
}) {
  return (
    <div className="prose-answer text-sm leading-6" data-testid={testId}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        // Raw HTML in model output is never rendered (react-markdown escapes it by default);
        // only http(s) links and our citation anchors survive.
        urlTransform={(url) => (url.startsWith(CITE_PREFIX) || /^https?:\/\//i.test(url) ? url : "")}
        components={{
          a({ href, children }) {
            if (href?.startsWith(CITE_PREFIX)) {
              const c = parseCitation(decodeURIComponent(href.slice(CITE_PREFIX.length)));
              if (c)
                return (
                  <button
                    type="button"
                    onClick={() => onCite(c)}
                    className="mx-0.5 inline-flex items-center rounded border border-accent/40 bg-accent/10 px-1.5 py-px font-mono text-[11px] text-accent hover:bg-accent/20"
                    data-testid="citation-chip"
                    title={`Open ${c.path} lines ${c.start}-${c.end}`}
                  >
                    {c.path}:{c.start}
                    {c.end !== c.start ? `-${c.end}` : ""}
                  </button>
                );
            }
            if (!href) return <span>{children}</span>;
            return (
              <a href={href} target="_blank" rel="noopener noreferrer nofollow" className="text-accent underline">
                {children}
              </a>
            );
          },
        }}
      >
        {linkCitations(text)}
      </ReactMarkdown>
    </div>
  );
}

export function VerificationBadge({ v }: { v: AnswerResult["verification"] }) {
  if (!v || v.skipped) return null;
  const ok = v.ok;
  const label = ok
    ? `${v.citations_total ?? 0} citations verified`
    : `${v.invalid_after_revision ?? 0} invalid citation(s), ${v.hallucinated_symbols_after ?? 0} unknown symbol(s)`;
  const details = [
    v.revised ? "Revised once after verification." : null,
    ...(v.invalid_citations ?? []).map((c) => `${c.citation}: ${c.reason}`),
    ...(v.hallucinated_symbols ?? []).map((s) => `Unknown identifier: ${s}`),
  ].filter(Boolean);
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${ok ? "bg-ok/15 text-ok" : "bg-warn/15 text-warn"}`}
      title={details.join("\n") || undefined}
      data-testid="verification-badge"
    >
      {ok ? "✓" : "!"} {label}
      {v.revised ? " (revised)" : ""}
    </span>
  );
}

export function UsageLine({ r }: { r: AnswerResult }) {
  const u = r.usage;
  return (
    <p className="text-xs text-muted" data-testid="usage">
      {u.total_tokens.toLocaleString()} tokens · {u.cost_usd == null ? "cost n/a" : `$${u.cost_usd.toFixed(4)}`} ·{" "}
      {u.latency_s.toFixed(1)}s · {u.tool_calls} tool calls · {u.llm_calls} LLM calls
      {r.budget_exhausted ? " · budget exhausted" : ""}
    </p>
  );
}
