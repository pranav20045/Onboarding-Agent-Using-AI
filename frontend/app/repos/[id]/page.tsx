"use client";

import Link from "next/link";
import { use, useEffect, useState } from "react";
import { api, type Citation, type Repo, type SystemName, SYSTEMS } from "@/lib/api";
import { CodeViewer } from "@/components/CodeViewer";
import { SystemRun } from "@/components/SystemRun";
import { Card, ErrorBox, Spinner, StatusPill } from "@/components/ui";

interface Turn {
  id: number;
  question: string;
  systems: SystemName[];
}

const EXAMPLES = [
  "What happens when a request is sent? Trace the call chain.",
  "Where is retry logic configured?",
  "Why was this behaviour changed? Look at the history.",
];

export default function RepoPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [repo, setRepo] = useState<Repo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [system, setSystem] = useState<SystemName>("indexed_agent");
  const [compare, setCompare] = useState(false);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [citation, setCitation] = useState<Citation | null>(null);

  useEffect(() => {
    api.getRepo(id).then(setRepo, (e: Error) => setError(e.message));
  }, [id]);

  function ask(e: React.FormEvent) {
    e.preventDefault();
    const q = question.trim();
    if (!q) return;
    setTurns((t) => [{ id: Date.now(), question: q, systems: compare ? SYSTEMS.map((s) => s.id) : [system] }, ...t]);
    setQuestion("");
  }

  if (error) return <ErrorBox message={error} />;
  if (!repo) return <Spinner label="Loading repository" />;
  const ready = Boolean(repo.indexed_sha);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold">
          {repo.owner}/{repo.name}
        </h1>
        <StatusPill status={repo.status} />
        {repo.indexed_sha && <span className="font-mono text-xs text-muted">@ {repo.indexed_sha.slice(0, 12)}</span>}
        <Link href={`/repos/${id}/tour`} className="ml-auto text-sm text-accent hover:underline">
          Onboarding tour →
        </Link>
      </div>

      {!ready ? (
        <p className="text-sm text-muted">This repository is still being indexed. Come back when it is ready.</p>
      ) : (
        <Card>
          <form onSubmit={ask} className="grid gap-3" aria-label="Ask a question">
            <label className="grid gap-1 text-sm">
              <span className="text-muted">Question</span>
              <textarea
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) ask(e);
                }}
                rows={3}
                maxLength={2000}
                placeholder={EXAMPLES[0]}
                className="rounded-md border border-line bg-bg px-3 py-2 text-fg"
                name="question"
              />
            </label>
            <div className="flex flex-wrap items-center gap-3 text-sm">
              <label className="flex items-center gap-2">
                <span className="text-muted">System</span>
                <select
                  value={system}
                  onChange={(e) => setSystem(e.target.value as SystemName)}
                  disabled={compare}
                  className="rounded-md border border-line bg-bg px-2 py-1.5"
                  name="system"
                >
                  {SYSTEMS.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={compare} onChange={(e) => setCompare(e.target.checked)} name="compare" />
                Compare all systems side by side
              </label>
              <button
                type="submit"
                className="ml-auto rounded-md bg-accent px-4 py-2 font-medium text-accent-fg disabled:opacity-50"
                disabled={!question.trim()}
              >
                Ask
              </button>
            </div>
          </form>
        </Card>
      )}

      {turns.length === 0 && ready && (
        <div className="text-sm text-muted">
          <p>Try:</p>
          <ul className="mt-1 list-disc pl-5">
            {EXAMPLES.map((ex) => (
              <li key={ex}>
                <button type="button" className="text-left hover:text-fg" onClick={() => setQuestion(ex)}>
                  {ex}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="space-y-4">
        {turns.map((t) => (
          <Card key={t.id}>
            <p className="mb-3 font-medium">{t.question}</p>
            <div className={t.systems.length > 1 ? "grid gap-6 lg:grid-cols-3" : ""}>
              {t.systems.map((s) => (
                <SystemRun key={s} repoId={id} question={t.question} system={s} onCite={setCitation} />
              ))}
            </div>
          </Card>
        ))}
      </div>

      {citation && <CodeViewer repoId={id} citation={citation} onClose={() => setCitation(null)} />}
    </div>
  );
}
