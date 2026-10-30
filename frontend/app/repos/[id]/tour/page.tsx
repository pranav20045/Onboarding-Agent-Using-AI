"use client";

import Link from "next/link";
import { use, useEffect, useState } from "react";
import { api, parseCitation, type Citation, type Tour } from "@/lib/api";
import { AnswerMarkdown } from "@/components/Answer";
import { CodeViewer } from "@/components/CodeViewer";
import { Card, ErrorBox, Spinner } from "@/components/ui";

function Cite({ raw, onCite }: { raw: string; onCite: (c: Citation) => void }) {
  const c = parseCitation(raw);
  if (!c) return <span className="font-mono text-xs">{raw}</span>;
  return (
    <button
      type="button"
      onClick={() => onCite(c)}
      className="rounded border border-accent/40 bg-accent/10 px-1.5 py-px font-mono text-[11px] text-accent hover:bg-accent/20"
      data-testid="citation-chip"
    >
      {c.path}:{c.start}-{c.end}
    </button>
  );
}

export default function TourPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [tour, setTour] = useState<Tour | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [citation, setCitation] = useState<Citation | null>(null);

  useEffect(() => {
    api.getTour(id).then(setTour, (e: Error) => setError(e.message));
  }, [id]);

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold">Onboarding tour</h1>
        <Link href={`/repos/${id}`} className="ml-auto text-sm text-accent hover:underline">
          ← Ask questions
        </Link>
      </div>
      {error && <ErrorBox message={error} />}
      {!tour && !error && <Spinner label="Building the tour (first time can take a minute)" />}
      {tour && (
        <>
          {tour.warnings.map((w) => (
            <p key={w} className="text-xs text-warn">
              {w}
            </p>
          ))}
          <Card>
            <h2 className="mb-2 font-semibold">What this repository is</h2>
            <AnswerMarkdown text={tour.purpose || "No README found."} onCite={setCitation} />
          </Card>

          <Card>
            <h2 className="mb-3 font-semibold">Suggested reading order</h2>
            <ol className="space-y-4" data-testid="reading-order">
              {tour.reading_order.map((s, i) => (
                <li key={`${s.citation}-${i}`} className="grid gap-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="flex h-6 w-6 items-center justify-center rounded-full bg-accent/15 text-xs font-semibold text-accent">
                      {i + 1}
                    </span>
                    <span className="font-medium">{s.title}</span>
                    <Cite raw={s.citation} onCite={setCitation} />
                  </div>
                  <p className="pl-8 text-sm text-muted">{s.explanation}</p>
                </li>
              ))}
            </ol>
          </Card>

          <div className="grid gap-4 md:grid-cols-2">
            <Card>
              <h2 className="mb-2 font-semibold">Entry points</h2>
              {tour.entry_points.length === 0 && <p className="text-sm text-muted">None detected.</p>}
              <ul className="space-y-1 text-sm">
                {tour.entry_points.map((e) => (
                  <li key={e.citation + e.detail} className="flex flex-wrap items-center gap-2">
                    <span className="rounded bg-line px-1.5 text-xs">{e.kind}</span>
                    <span className="break-all">{e.detail}</span>
                    <Cite raw={e.citation} onCite={setCitation} />
                  </li>
                ))}
              </ul>
            </Card>
            <Card>
              <h2 className="mb-2 font-semibold">Key classes and functions</h2>
              <ul className="space-y-1 text-sm">
                {tour.key_symbols.map((s) => (
                  <li key={s.qualified_name} className="flex flex-wrap items-center gap-2">
                    <span className="break-all font-mono text-xs">{s.qualified_name}</span>
                    <Cite raw={s.citation} onCite={setCitation} />
                  </li>
                ))}
              </ul>
            </Card>
            <Card>
              <h2 className="mb-2 font-semibold">Main packages</h2>
              <ul className="space-y-1 text-sm">
                {tour.packages.map((p) => (
                  <li key={p.path}>
                    <span className="font-mono">{p.path}</span> <span className="text-muted">({p.python_files} files)</span>
                  </li>
                ))}
              </ul>
            </Card>
            <Card>
              <h2 className="mb-2 font-semibold">Data models</h2>
              {tour.data_models.length === 0 && <p className="text-sm text-muted">None detected.</p>}
              <ul className="space-y-1 text-sm">
                {tour.data_models.map((m) => (
                  <li key={m.qualified_name} className="flex flex-wrap items-center gap-2">
                    <span className="break-all font-mono text-xs">{m.qualified_name}</span>
                    <Cite raw={m.citation} onCite={setCitation} />
                  </li>
                ))}
              </ul>
            </Card>
          </div>
          <p className="text-xs text-muted">
            Generated {new Date(tour.generated_at).toLocaleString()} for commit {tour.commit?.slice(0, 12)}
            {tour.llm ? ` · narrative by ${tour.llm.model}` : " · index-derived (no LLM)"}
          </p>
        </>
      )}
      {citation && <CodeViewer repoId={id} citation={citation} onClose={() => setCitation(null)} />}
    </div>
  );
}
