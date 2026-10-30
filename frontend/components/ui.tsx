// Small shared presentational pieces.

export function Card({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <div className={`rounded-xl border border-line bg-card p-4 ${className}`}>{children}</div>;
}

const STATUS_STYLE: Record<string, string> = {
  ready: "bg-ok/15 text-ok",
  failed: "bg-bad/15 text-bad",
  queued: "bg-muted/15 text-muted",
  indexing: "bg-warn/15 text-warn",
};

export function StatusPill({ status }: { status: string }) {
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_STYLE[status] ?? "bg-warn/15 text-warn"}`}>
      {status}
    </span>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-sm text-muted" role="status">
      <span className="h-3 w-3 animate-spin rounded-full border-2 border-muted border-t-transparent" aria-hidden />
      {label}
    </span>
  );
}

export function ErrorBox({ message }: { message: string }) {
  return (
    <div role="alert" data-testid="error" className="rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-bad">
      {message}
    </div>
  );
}
