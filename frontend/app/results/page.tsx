"use client";

import { useEffect, useState } from "react";
import { api, API_URL, ApiError, type ResultsSummary } from "@/lib/api";
import { Card, ErrorBox, Spinner } from "@/components/ui";

const CATS = ["locate", "explain", "flow", "why", "unanswerable"];
const CHART_TITLES: Record<string, string> = {
  "correctness_vs_cost.png": "Correctness vs cost (mean judge score against mean cost per question)",
  "score_by_category.png": "Mean judge score by question category",
};

function num(v: unknown, kind: "pct" | "score" | "money" | "sec" = "score"): string {
  if (typeof v !== "number") return "–";
  if (kind === "pct") return `${(v * 100).toFixed(0)}%`;
  if (kind === "money") return `$${v.toFixed(4)}`;
  if (kind === "sec") return `${v.toFixed(1)}s`;
  return v.toFixed(2);
}

export default function ResultsPage() {
  const [data, setData] = useState<ResultsSummary | null>(null);
  const [empty, setEmpty] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.getResults().then(setData, (e) => {
      if (e instanceof ApiError && e.status === 404) setEmpty(true);
      else setError((e as Error).message);
    });
  }, []);

  if (error) return <ErrorBox message={error} />;
  if (empty) return <p className="text-sm text-muted">No evaluation results have been published yet.</p>;
  if (!data) return <Spinner label="Loading results" />;
  if (data.runs.length === 0) return <p className="text-sm text-muted">No evaluation results have been published yet.</p>;
  const labels = Object.keys(data.aggregate);
  const mock = data.runs.some((r) => r.mock);

  return (
    <div className="space-y-5">
      <h1 className="text-xl font-semibold">Evaluation results</h1>
      <p className="text-sm text-muted">
        Index-first vs explore-on-demand. Scores are LLM-judge scores (0 wrong, 1 partial, 2 correct). Computed from
        saved raw runs by <code>eval.summarize</code>.
      </p>
      {mock && <ErrorBox message="These results include MOCK runs (offline pipeline check). The numbers are meaningless." />}
      {data.fairness_warnings.map((w) => (
        <p key={w} className="text-sm text-warn">
          ⚠ {w}
        </p>
      ))}
      <Card className="overflow-x-auto">
        <table className="w-full min-w-[720px] text-left text-sm" data-testid="results-table">
          <thead className="text-xs text-muted">
            <tr>
              {["System", "n", "Mean score", "Correct", "Refusal (unanswerable)", "Recall@5", "Gold cited", "Cost/q", "p50 / p95", "Tools"].map(
                (h) => (
                  <th key={h} className="border-b border-line px-2 py-2 font-medium">
                    {h}
                  </th>
                ),
              )}
            </tr>
          </thead>
          <tbody>
            {labels.map((l) => {
              const a = data.aggregate[l];
              return (
                <tr key={l} className="border-b border-line/60">
                  <td className="px-2 py-2 font-medium">{l}</td>
                  <td className="px-2">{num(a.n, "score").replace(".00", "")}</td>
                  <td className="px-2">{num(a.mean_score)}</td>
                  <td className="px-2">{num(a.pct_correct, "pct")}</td>
                  <td className="px-2">{num(a.refusal_rate_unanswerable, "pct")}</td>
                  <td className="px-2">{num(a["recall@5"], "pct")}</td>
                  <td className="px-2">{num(a.gold_citation_hit_rate, "pct")}</td>
                  <td className="px-2">{num(a.mean_cost_usd, "money")}</td>
                  <td className="px-2">
                    {num(a.latency_p50_s, "sec")} / {num(a.latency_p95_s, "sec")}
                  </td>
                  <td className="px-2">{num(a.mean_tool_calls)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>
      <Card className="overflow-x-auto">
        <h2 className="mb-2 font-semibold">Mean score by category</h2>
        <table className="w-full min-w-[560px] text-left text-sm">
          <thead className="text-xs text-muted">
            <tr>
              <th className="px-2 py-2">System</th>
              {CATS.map((c) => (
                <th key={c} className="px-2 py-2">
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {labels.map((l) => (
              <tr key={l} className="border-t border-line/60">
                <td className="px-2 py-2 font-medium">{l}</td>
                {CATS.map((c) => {
                  const cell = data.by_category[l]?.[c];
                  return (
                    <td key={c} className="px-2">
                      {cell ? `${num(cell.mean_score)} (n=${cell.n})` : "–"}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
      {data.charts.length > 0 && (
        <div className="grid gap-4 md:grid-cols-2" data-testid="charts">
          {data.charts.map((name) => (
            <Card key={name}>
              <h2 className="mb-2 text-sm font-semibold">{CHART_TITLES[name] ?? name}</h2>
              {/* eslint-disable-next-line @next/next/no-img-element -- served by the API, not optimizable */}
              <img
                src={`${API_URL}/results/charts/${encodeURIComponent(name)}`}
                alt={CHART_TITLES[name] ?? name}
                className="h-auto w-full rounded bg-white"
                loading="lazy"
              />
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
