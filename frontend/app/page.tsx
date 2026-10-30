"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type Repo } from "@/lib/api";
import { Card, ErrorBox, Spinner, StatusPill } from "@/components/ui";

const ACTIVE = new Set(["queued", "indexing"]);

function progress(repo: Repo): string {
  const j = repo.job;
  if (!j || !ACTIVE.has(repo.status)) return "";
  return j.progress_total ? `${j.status} ${j.progress_done}/${j.progress_total}` : j.status;
}

export default function Home() {
  const [repos, setRepos] = useState<Repo[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [url, setUrl] = useState("");
  const [sha, setSha] = useState("");
  const [token, setToken] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setRepos(await api.listRepos());
      setLoadError(null);
    } catch (e) {
      setLoadError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Poll while any repo is indexing so progress updates live.
  const busy = repos?.some((r) => ACTIVE.has(r.status)) ?? false;
  useEffect(() => {
    if (!busy) return;
    const id = setInterval(load, 1500);
    return () => clearInterval(id);
  }, [busy, load]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setFormError(null);
    try {
      await api.addRepo(url.trim(), sha.trim() || undefined, token.trim() || undefined);
      setUrl("");
      setSha("");
      await load();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Could not add the repository");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="space-y-6">
      <section>
        <h1 className="text-2xl font-semibold">Repositories</h1>
        <p className="mt-1 text-sm text-muted">
          Index a public GitHub Python repository, then ask questions and get answers with exact citations.
        </p>
      </section>

      <Card>
        <form onSubmit={submit} className="grid gap-3 sm:grid-cols-[1fr_12rem_auto]" aria-label="Add repository">
          <label className="grid gap-1 text-sm">
            <span className="text-muted">GitHub URL</span>
            <input
              required
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://github.com/pallets/flask"
              className="rounded-md border border-line bg-bg px-3 py-2 text-fg"
              name="url"
            />
          </label>
          <label className="grid gap-1 text-sm">
            <span className="text-muted">Commit (optional)</span>
            <input
              value={sha}
              onChange={(e) => setSha(e.target.value)}
              placeholder="pin to a SHA"
              className="rounded-md border border-line bg-bg px-3 py-2 font-mono text-fg"
              name="commit"
            />
          </label>
          <button
            type="submit"
            disabled={submitting}
            className="self-end rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-fg disabled:opacity-50"
          >
            {submitting ? "Adding…" : "Add repository"}
          </button>
          <details className="text-sm text-muted sm:col-span-3">
            <summary className="cursor-pointer">Admin token</summary>
            <input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="Only needed if the server sets ADMIN_TOKEN"
              className="mt-2 w-full rounded-md border border-line bg-bg px-3 py-2 text-fg sm:w-96"
              aria-label="Admin token"
            />
          </details>
          {formError && (
            <div className="sm:col-span-3">
              <ErrorBox message={formError} />
            </div>
          )}
        </form>
      </Card>

      {loadError && <ErrorBox message={loadError} />}
      {repos === null && !loadError && <Spinner label="Loading repositories" />}
      {repos?.length === 0 && <p className="text-sm text-muted">No repositories yet. Add one above to get started.</p>}

      <ul className="grid gap-3 md:grid-cols-2" aria-label="Indexed repositories">
        {repos?.map((r) => (
          <li key={r.id}>
            <Card className="h-full">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">
                  {r.owner}/{r.name}
                </span>
                <StatusPill status={r.status} />
                {ACTIVE.has(r.status) && <Spinner label={progress(r)} />}
              </div>
              {r.indexed_sha && <p className="mt-1 font-mono text-xs text-muted">@ {r.indexed_sha.slice(0, 12)}</p>}
              {r.stats && (
                <p className="mt-2 text-sm text-muted">
                  {r.stats.files_python ?? 0} Python files · {Object.values(r.stats.units ?? {}).reduce((a, b) => a + b, 0)} code
                  units
                  {r.stats.resolution != null && ` · ${(r.stats.resolution * 100).toFixed(0)}% calls resolved`}
                </p>
              )}
              {r.status === "failed" && r.error && <p className="mt-2 text-sm text-bad">{r.error}</p>}
              {r.indexed_sha && (
                <div className="mt-3 flex gap-3 text-sm">
                  <Link className="text-accent hover:underline" href={`/repos/${r.id}`}>
                    Ask questions
                  </Link>
                  <Link className="text-accent hover:underline" href={`/repos/${r.id}/tour`}>
                    Onboarding tour
                  </Link>
                </div>
              )}
            </Card>
          </li>
        ))}
      </ul>
    </div>
  );
}
