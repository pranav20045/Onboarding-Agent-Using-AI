"use client";

import { useEffect, useRef, useState } from "react";
import { askStream, type AgentEvent, type AnswerResult, type Citation, type SystemName, SYSTEMS } from "@/lib/api";
import { AnswerMarkdown, UsageLine, VerificationBadge } from "./Answer";
import { ErrorBox, Spinner } from "./ui";

function stepLabel(e: AgentEvent): string {
  if (e.type === "tool_call") return e.label ?? `${e.tool}`;
  if (e.type === "budget_exhausted") return `Budget exhausted (${e.reason}); writing the final answer`;
  if (e.type === "verification") return e.ok ? "Citations verified" : "Verification found problems; revising";
  if (e.type === "revision") return e.ok ? "Revised answer verified" : "Revised answer still has problems";
  return e.type;
}

/** Streams one system's answer: live agent steps, then the answer with citations and usage. */
export function SystemRun({
  repoId,
  question,
  system,
  onCite,
}: {
  repoId: string;
  question: string;
  system: SystemName;
  onCite: (c: Citation) => void;
}) {
  const [steps, setSteps] = useState<AgentEvent[]>([]);
  const [draft, setDraft] = useState("");
  const [result, setResult] = useState<AnswerResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return; // StrictMode double-invoke guard
    started.current = true;
    const ctrl = new AbortController();
    askStream(
      repoId,
      question,
      system,
      (e) => {
        if (e.type === "answer_delta") {
          setDraft((d) => d + (e.text ?? ""));
        } else if (e.type === "turn_end") {
          // A turn that ended in tool calls was narration ("let me search..."), not the answer.
          if (!e.final) setDraft("");
        } else if (["tool_call", "budget_exhausted", "verification", "revision"].includes(e.type)) {
          setSteps((s) => [...s, e]);
        } else if (e.type === "tool_result_summary") {
          setSteps((s) => {
            const copy = [...s];
            for (let i = copy.length - 1; i >= 0; i--) {
              if (copy[i].type === "tool_call" && copy[i].result === undefined) {
                copy[i] = { ...copy[i], result: e.summary, is_error: e.is_error };
                break;
              }
            }
            return copy;
          });
        } else if (e.type === "result") {
          setResult(e as unknown as AnswerResult);
          setDraft(""); // the verified answer replaces the live draft (it may have been revised)
        } else if (e.type === "error") {
          setError(e.message ?? "Answering failed");
        } else if (e.type === "done") {
          setDone(true);
        }
      },
      ctrl.signal,
    )
      .catch((e: Error) => setError(e.message))
      .finally(() => setDone(true));
    return () => ctrl.abort();
  }, [repoId, question, system]);

  const label = SYSTEMS.find((s) => s.id === system)?.label ?? system;
  return (
    <div className="flex min-w-0 flex-col gap-3" data-testid={`run-${system}`}>
      <div className="flex items-center gap-2">
        <h3 className="text-sm font-semibold">{label}</h3>
        {!done && <Spinner label="Working" />}
        {result && <VerificationBadge v={result.verification} />}
      </div>
      {steps.length > 0 && (
        <ol className="space-y-1 border-l-2 border-line pl-3 text-xs text-muted" aria-label="Agent steps" data-testid="agent-steps">
          {steps.map((s, i) => (
            <li key={i}>
              <span className={s.is_error ? "text-bad" : ""}>{stepLabel(s)}</span>
              {typeof s.result === "string" && <span className="text-muted/80"> — {s.result}</span>}
            </li>
          ))}
        </ol>
      )}
      {error && <ErrorBox message={error} />}
      {!result && draft && (
        <div aria-live="polite" aria-label="Answer being written">
          <AnswerMarkdown text={draft} onCite={onCite} testId="draft" />
          <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-accent align-middle" aria-hidden />
        </div>
      )}
      {result && (
        <>
          {result.error && <ErrorBox message={result.error} />}
          {result.answer ? (
            <AnswerMarkdown text={result.answer} onCite={onCite} />
          ) : (
            !result.error && <p className="text-sm text-muted">No answer was produced.</p>
          )}
          <UsageLine r={result} />
        </>
      )}
    </div>
  );
}
