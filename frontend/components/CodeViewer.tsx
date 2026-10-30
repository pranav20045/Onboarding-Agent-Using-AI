"use client";

import { useEffect, useRef, useState } from "react";
import { api, type Citation } from "@/lib/api";
import { ErrorBox, Spinner } from "./ui";

const CONTEXT = 15; // lines shown around the cited range

function languageFor(path: string): string {
  if (path.endsWith(".py") || path.endsWith(".pyi")) return "python";
  if (path.endsWith(".md")) return "markdown";
  if (path.endsWith(".toml")) return "toml";
  if (path.endsWith(".yml") || path.endsWith(".yaml")) return "yaml";
  if (path.endsWith(".json")) return "json";
  return "text";
}

/** Side panel showing the cited file with the exact lines highlighted (Shiki). */
export function CodeViewer({ repoId, citation, onClose }: { repoId: string; citation: Citation; onClose: () => void }) {
  const [html, setHtml] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [meta, setMeta] = useState<{ start: number; end: number; total: number } | null>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    setHtml(null);
    setError(null);
    (async () => {
      try {
        const from = Math.max(1, citation.start - CONTEXT);
        const file = await api.getFile(repoId, citation.path, from, citation.end + CONTEXT);
        const { codeToHtml } = await import("shiki");
        const out = await codeToHtml(file.lines.join("\n"), {
          lang: languageFor(file.path),
          themes: { light: "github-light", dark: "github-dark" },
          transformers: [
            {
              line(node, line) {
                const n = file.start + line - 1;
                node.properties["data-line"] = String(n);
                if (n >= citation.start && n <= citation.end) this.addClassToHast(node, "hl");
              },
            },
          ],
        });
        if (!cancelled) {
          setHtml(out); // Shiki escapes the source text, so this HTML is safe to inject.
          setMeta({ start: file.start, end: file.end, total: file.total_lines });
        }
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [repoId, citation]);

  useEffect(() => {
    if (!html) return;
    panelRef.current?.querySelector(".line.hl")?.scrollIntoView({ block: "center" });
  }, [html]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <aside
      className="fixed inset-y-0 right-0 z-30 flex w-full max-w-3xl flex-col border-l border-line bg-card shadow-2xl"
      aria-label="Code viewer"
      data-testid="code-viewer"
    >
      <div className="flex items-center gap-3 border-b border-line px-4 py-3">
        <div className="min-w-0">
          <p className="truncate font-mono text-sm" data-testid="viewer-path">
            {citation.path}
          </p>
          <p className="text-xs text-muted" data-testid="viewer-range">
            lines {citation.start}–{citation.end}
            {meta && ` · file has ${meta.total} lines`}
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="ml-auto rounded-md border border-line px-2.5 py-1 text-sm"
          aria-label="Close code viewer"
          autoFocus
        >
          Close
        </button>
      </div>
      <div ref={panelRef} className="flex-1 overflow-auto p-2 font-mono text-[13px] leading-5">
        {error && <ErrorBox message={error} />}
        {!html && !error && <Spinner label="Loading file" />}
        {html && <div data-testid="viewer-code" dangerouslySetInnerHTML={{ __html: html }} />}
      </div>
    </aside>
  );
}
