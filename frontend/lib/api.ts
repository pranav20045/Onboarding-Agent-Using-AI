// Typed client for the CodeAtlas API, including an SSE reader for POST /repos/{id}/ask.

export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

export type SystemName = "indexed_agent" | "grep_agent" | "naive_rag";
export const SYSTEMS: { id: SystemName; label: string }[] = [
  { id: "indexed_agent", label: "Indexed agent" },
  { id: "grep_agent", label: "Grep agent" },
  { id: "naive_rag", label: "Naive RAG" },
];

export interface Job {
  id: string;
  kind: string;
  status: string;
  progress_done: number;
  progress_total: number;
  error: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface Repo {
  id: string;
  url: string;
  owner: string;
  name: string;
  status: "queued" | "indexing" | "ready" | "failed";
  error: string | null;
  pinned_sha: string | null;
  indexed_sha: string | null;
  updated_at: string;
  stats: {
    files_python?: number;
    files_text?: number;
    units?: Record<string, number>;
    resolution?: number | null;
    resolution_non_test?: number | null;
  } | null;
  job: Job | null;
}

export interface Citation {
  path: string;
  start: number;
  end: number;
  raw: string;
}

export interface Usage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number | null;
  llm_calls: number;
  tool_calls: number;
  latency_s: number;
  models: string[];
}

export interface Verification {
  ok?: boolean;
  skipped?: boolean;
  citations_total?: number;
  invalid_before_revision?: number;
  invalid_after_revision?: number;
  hallucinated_symbols_before?: number;
  hallucinated_symbols_after?: number;
  hallucinated_symbols?: string[];
  invalid_citations?: { citation: string; reason: string }[];
  revised?: boolean;
}

export interface AnswerResult {
  system: SystemName;
  answer: string;
  citations: Citation[];
  verification: Verification;
  usage: Usage;
  retrieved_files: string[];
  budget_exhausted: boolean;
  error: string | null;
}

export interface AgentEvent {
  type: string;
  t?: number;
  tool?: string;
  label?: string;
  summary?: string;
  is_error?: boolean;
  truncated?: boolean;
  text?: string;
  message?: string;
  reason?: string;
  [key: string]: unknown;
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
      cache: "no-store",
    });
  } catch {
    throw new ApiError(0, "network_error", `Cannot reach the API at ${API_URL}`);
  }
  if (!res.ok) {
    let code = "http_error";
    let message = res.statusText;
    try {
      const body = await res.json();
      code = body?.error?.code ?? code;
      message = body?.error?.message ?? message;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, code, message);
  }
  return res.json() as Promise<T>;
}

export const api = {
  listRepos: () => request<{ repos: Repo[] }>("/repos").then((r) => r.repos),
  getRepo: (id: string) => request<Repo>(`/repos/${encodeURIComponent(id)}`),
  addRepo: (url: string, commitSha?: string, adminToken?: string) =>
    request<{ repo_id: string; job_id: string }>("/repos", {
      method: "POST",
      body: JSON.stringify({ url, commit_sha: commitSha || null }),
      headers: adminToken ? { Authorization: `Bearer ${adminToken}` } : undefined,
    }),
  getFile: (id: string, path: string, start?: number, end?: number) => {
    const q = new URLSearchParams({ path });
    if (start) q.set("start", String(start));
    if (end) q.set("end", String(end));
    return request<{ path: string; start: number; end: number; total_lines: number; lines: string[] }>(
      `/repos/${encodeURIComponent(id)}/file?${q}`,
    );
  },
  getTour: (id: string, refresh = false) =>
    request<Tour>(`/repos/${encodeURIComponent(id)}/tour${refresh ? "?refresh=true" : ""}`),
  getResults: () => request<ResultsSummary>("/results"),
};

export interface TourStep {
  title: string;
  citation: string;
  explanation: string;
}

export interface Tour {
  repo_id: string;
  commit: string | null;
  generated_at: string;
  purpose: string;
  packages: { path: string; python_files: number }[];
  entry_points: { kind: string; detail: string; qualified_name: string | null; citation: string }[];
  key_symbols: { qualified_name: string; kind: string; signature: string; citation: string }[];
  data_models: { qualified_name: string; citation: string }[];
  reading_order: TourStep[];
  llm: { model: string } | null;
  warnings: string[];
}

export interface ResultsSummary {
  runs: { run_id: string; label: string; model: string; judge_model: string; n: number; mock: boolean }[];
  aggregate: Record<string, Record<string, number | null | number[]>>;
  by_category: Record<string, Record<string, Record<string, number | null>>>;
  fairness_warnings: string[];
  charts: string[];
}

/** Stream an answer. Calls `onEvent` for every SSE event; resolves when the stream ends. */
export async function askStream(
  repoId: string,
  question: string,
  system: SystemName,
  onEvent: (e: AgentEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}/repos/${encodeURIComponent(repoId)}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ question, system }),
      signal,
    });
  } catch (e) {
    if ((e as Error).name === "AbortError") return;
    throw new ApiError(0, "network_error", `Cannot reach the API at ${API_URL}`);
  }
  if (!res.ok || !res.body) {
    let message = res.statusText;
    try {
      message = (await res.json())?.error?.message ?? message;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, "ask_failed", message);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buffer.search(/\r?\n\r?\n/)) !== -1) {
      const block = buffer.slice(0, idx);
      buffer = buffer.slice(idx).replace(/^\r?\n\r?\n/, "");
      const data = block
        .split(/\r?\n/)
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trimStart())
        .join("\n");
      if (data) {
        try {
          onEvent(JSON.parse(data) as AgentEvent);
        } catch {
          /* ignore malformed event */
        }
      }
    }
  }
}

export const CITATION_RE = /\[([A-Za-z0-9_./-]+\.?[A-Za-z0-9_-]*):(\d+)(?:-(\d+))?\]/g;

export function parseCitation(raw: string): Citation | null {
  const m = /^\[?([A-Za-z0-9_./-]+\.?[A-Za-z0-9_-]*):(\d+)(?:-(\d+))?\]?$/.exec(raw.trim());
  if (!m) return null;
  const start = Number(m[2]);
  return { path: m[1], start, end: m[3] ? Number(m[3]) : start, raw };
}
