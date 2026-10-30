# Progress

What exists and what has actually been run, phase by phase (BUILD_PROMPT.md §7). Anything not
run is listed under "Not done / not verified". Last updated 2026-10-27.

| Phase | Status |
|---|---|
| 0 — Setup | Done |
| 1 — Ingestion, parsing, graph | Done |
| 2 — Indexing, retrieval, naive_rag | Done (no real-LLM call yet) |
| 3 — indexed_agent | Done (mocked-LLM tests only) |
| 4 — History | Done (GitHub linking limited without a token) |
| 5 — grep_agent | Done (mocked-LLM tests only) |
| 6 — API | Done |
| 7 — Frontend | Done |
| 8 — Eval harness | Done; datasets drafted, **awaiting human review**; dry run done with a **mock** LLM |
| 9 — Tour | Done |
| 10 — Full eval, deployment, docs | **Partial**: Docker + docs done; experiments **not run** (need verified data + API key) |

## Verified by running

- **Backend tests:** `cd backend && uv run pytest` → 315 passed, 0 failed, 4 skipped (skips: symlink tests that need Windows symlink privileges, and two QA checks that need a git checkout). `ruff check` and `ruff format --check` clean.
- **Frontend:** `npm run typecheck` clean; `next build` succeeds; `npm audit --omit=dev` 0 vulnerabilities; Playwright: 14 desktop + 1 mobile (390 px) smoke tests pass against the real API with offline stand-ins — add repo with live progress, ask with live agent steps, click a citation and check the viewer shows the right file and exactly the cited lines, compare view, XSS payload not rendered, tour with working citations, results empty state, error state, dark mode, axe (no serious/critical WCAG A/AA violations in light and dark), no horizontal scroll on mobile.
- **Real index of `psf/requests@611c6162`** (real git clone, tree-sitter, fastembed models, Docker Qdrant): 37 Python + 57 text files; 96 classes, 187 functions, 524 methods; call resolution 58.6% (53.6% non-test) of 2,596 calls; 874 AST + 399 fixed chunks; 6,495 commits mined, 18 pre-linked to PRs before the anonymous GitHub rate limit; ~17 min total, ~16 of it CPU embedding.
- **Real index of `pallets/flask@d086db85`:** 83 Python + 111 text files; 160 classes, 1,060 functions, 402 methods; call resolution 40.8% (38.1% non-test) of 3,791 calls; 1,207 AST + 715 fixed chunks; 5,562 commits mined, 0 pre-linked (anonymous GitHub quota already used); ~55 min, almost all CPU embedding.
- **`fastapi/fastapi@7a0a3f0b`:** cloned, parsed (1,219 Python files) and history-mined; **embedding was still running** when this file was last updated (`data/index_fastapi_fastapi.err` shows progress; re-run `python -m app.cli index https://github.com/fastapi/fastapi --commit 7a0a3f0b286c6c865cf4398ea0e957a87b8bffa4` to resume, it is incremental).
- **Eval dry run** (`eval/results/dryrun/`): 5 requests questions × naive_rag / indexed_agent / grep_agent with `--mock-llm` (offline deterministic answers + heuristic judge) → raw runs, judgments, metrics, `summary.md`, charts. Proves the pipeline; the numbers are meaningless by design.
- **Datasets:** 3 × 60 drafted questions (12 per category), all pass `eval/datasets/schema.py` validation at their pinned commits; 0 verified.
- **Docker:** `docker compose config` validates; `backend.Dockerfile` builds (1.25 GB) and the container serves `/health`, returns the JSON error format, runs as non-root, and has ripgrep + git. `docker compose up -d --build` brings up Qdrant (pinned by digest), backend and frontend: `/health` ok, UI returns 200, `/results` serves the summary. The `frontend.Dockerfile` standalone image also builds. Not done in compose: indexing a repo inside the container (it would re-download ~700 MB of models into the new volume).
- **Concurrency stress test** (`qa/scripts/stress_init_db.py`, run from `backend/`): 0 failed of 60 processes for `init_db` and for `connect` (the audit saw 15/60 and 7–10/40 before the fix).
- **QA audit (Phase 0) findings:** F01, F01b, F02, F03, F04, F05, F06, F07, F08, F09, F10, SEC-01 (documented), SEC-02 fixed; the audit's strict-xfail reproductions now pass and their markers were removed. Details: DECISIONS.md, "Phase 0 audit fixes".

## Not done / not verified

- **No real LLM call has been made** by any system, the judge, or the tour: no `ANTHROPIC_API_KEY` is configured on the build machine. The Anthropic provider is tested against a stub SDK client (request shape, tool-call parsing, thinking-block replay, cost), not the live API. Real behaviour (prompt-injection resistance, refusals on unanswerable questions, citation quality) is unmeasured.
- **Experiments (§6.5) not run.** They need (1) a human pass over the 180 drafts with `eval/review.py` and (2) an API key. Estimated cost of the full matrix is printed by `eval.run` before each run; it will exceed the $2 guard, so it needs explicit approval.
- **Ollama experiment:** Ollama is not installed on the build machine; the provider is implemented and unit-tested via the OpenAI-compatible path only.
- **Judge calibration:** tools exist; no human grades yet, so no agreement number.
- **GitHub history linking** is shallow without `GITHUB_TOKEN` (15–47 commits pre-linked per repo).
- **Deployment** to a hosted service (Render/Railway/VM) not done; README covers Docker Compose only.
- **Demo GIF** placeholder only.
- **Langfuse tracing:** wrapper written against the installed SDK's API (`start_as_current_observation`), no-op when unset; never exercised against a Langfuse server.

## How to run

See README "Setup". Quick checks: `cd backend && uv run pytest`; `cd frontend && npm run test:e2e`.

## Added after the end-to-end build (2026-10-29)

- **Token-by-token answer streaming** (providers, loop, SSE, UI; tested at every layer incl. Playwright). Revision/tour/judge calls are not streamed.
- **Results page charts** (`GET /results/charts/{name}`, safe name check, tested).
- **Configurable ignore list** (`EXTRA_IGNORE_DIRS`, `EXTRA_IGNORE_GLOBS`, `UNIGNORE_DIRS`).
- **History pre-linking by most-changed + most-recent** commits.
- **Embedding OOM fix** found while indexing `fastapi` (length-aware batching + retry).
- Deliberately not done (your call): hosted deployment guide; per-step Langfuse tracing.
