# Decision log

Every notable design decision, the alternatives considered, and anything that did not work.
Format: date · decision · why · result. This feeds the README's "What I tried that didn't work".

---

### 2026-10-25 · Project lives at the repository root, not in a `codeatlas/` subfolder
- **Why:** the spec's tree shows `codeatlas/` as the repo itself. Keeping `BUILD_PROMPT.md` / `TEST_PROMPT.md` at the root means an auditor cloning the repo finds the spec next to the code.
- **Result:** OK.

### 2026-10-25 · Backend is a non-packaged uv project (`[tool.uv] package = false`)
- **Alternatives:** `uv init --app` default `src/` layout with a build backend.
- **Why:** the spec's layout is `backend/app/...`; it is an application, not a library to publish. Tests import `app.*` via `pytest.ini_options.pythonpath = ["."]`.
- **Result:** OK. All direct dependencies pinned with `uv add --bounds exact`; `uv.lock` pins transitives.

### 2026-10-25 · uv needs `UV_SYSTEM_CERTS=1` on this machine
- **What happened:** `uv add` failed with `invalid peer certificate: UnknownIssuer`. The network does TLS interception with a corporate root CA that is in the Windows store but not in uv's bundled roots.
- **Fix:** export `UV_SYSTEM_CERTS=1` (and `UV_NATIVE_TLS=1` for older uv). Documented in README setup. Not needed on a normal network.

### 2026-10-25 · SQLite via stdlib `sqlite3` + hand-written SQL migrations (no ORM)
- **Alternatives:** SQLAlchemy + Alembic; SQLModel.
- **Why:** not in the approved stack (rule 9: no unlisted major deps), and the schema is small. Plain SQL files numbered `NNNN_name.sql`, applied in order inside `BEGIN IMMEDIATE … COMMIT`, version tracked with `PRAGMA user_version`. Every line is explainable.
- **Gotcha:** `Connection.executescript()` issues an implicit `COMMIT` first, which would make a failing migration half-applied. So migrations are split on `;` and run statement-by-statement inside our own transaction. Consequence: migrations must not contain `;` inside string literals or triggers. A test proves a failing migration rolls back fully.
- **Pragmas:** `foreign_keys=ON` (cascade deletes repo → files → symbols), WAL (concurrent readers during background indexing), `busy_timeout=30s`.
- **Async:** sqlite3 is sync; API code will call it via `asyncio.to_thread` where it matters.

### 2026-10-25 · `repos.url` is UNIQUE (one entry per GitHub repo)
- **Alternatives:** unique on `(url, commit_sha)` so several pinned versions co-exist.
- **Why:** simpler for the team service; re-pinning updates the existing repo and triggers a re-index. For eval, each dataset pins one SHA per repo, so this is sufficient.
- **Revisit if:** we need to compare answers across commits of the same repo.

### 2026-10-25 · Settings: no env prefix, secrets as `SecretStr`
- **Why:** standard names (`ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, `LANGFUSE_*`) work without duplication. `SecretStr` keeps keys out of reprs, logs and error messages (audit item). A test asserts `.env.example` and `Settings` list exactly the same variables.
- `.env` is resolved relative to the repo root, not the CWD, so `backend/` and root invocations behave the same.

### 2026-10-25 · Fixture repo is built hermetically by a script
- **Why:** a real git history is needed for blame/history tests, and nested `.git` dirs can't be committed. `tests/fixtures/build_fixture_repo.py` creates it with `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, fixed author and dates, so **commit SHAs are identical on every machine** (tested). Tests build it once per session into a temp dir; the CLI builds it to `backend/tests/fixtures/.build/` (gitignored).
- **Contents:** 7 commits covering a design-rationale commit ("3 retries with backoff… Fixes #42"), a rename, a deletion, decorated/nested/async functions, a class hierarchy, `self.method()` and aliased-import calls, route decorators, `__main__`, console_scripts, a Python 2 file, a syntax-error file, a Latin-1 file, and a README with a prompt-injection string.

### 2026-10-25 · Qdrant image pinned to `v1.19.2`, not `v1.20.0`
- **Why:** `v1.20.0` was published 4 days ago (2026-10-21). Prefer the last patch release of the previous minor. The `qdrant-client` version will be chosen in Phase 2 to match the server minor.
- Ports bound to `127.0.0.1` only; optional API key via `QDRANT_API_KEY`; telemetry disabled.

### 2026-10-25 · Ruff with the `S` (bandit) rules enabled
- **Why:** catches `shell=True` (S602/S604/S605), `eval`, unsafe temp files etc., backing the "no shell strings / never execute repo code" rules. S603/S607 are ignored because list-argument `subprocess` calls on PATH-resolved `git`/`rg` are exactly the intended pattern.

---

## Phase 1 — Ingestion, parsing, graph

### 2026-10-25 · tree-sitter pinned to 0.25.2 (0.26.0 crashes on Windows)
- **What happened:** with `tree-sitter==0.26.0`, `Node.child_by_field_name()` caused a native *access violation* (process exit 0xC0000005) part-way through 9 of 37 files in `psf/requests` (e.g. `src/requests/auth.py`). Small snippets of the same code did not crash; the full file did, so it is a memory bug in the binding, not our traversal.
- **Fix:** pinned `tree-sitter==0.25.2` (+ `tree-sitter-python==0.25.0`). Same scan over all requests files: no crashes. Re-test 0.26.x when a patch release ships.

### 2026-10-25 · Official `tree-sitter` + `tree-sitter-python` instead of `tree-sitter-language-pack`
- **Why:** only Python is parsed structurally; one grammar wheel is smaller and has fewer moving parts.

### 2026-10-25 · Python 3 validity decided by `ast.parse`, units by tree-sitter
- **What happened:** the tree-sitter grammar is error-tolerant and parses Python 2 `print "x"` as a `print_statement` *without* setting `has_error`, so it can't tell us a file is invalid.
- **Decision:** `ast.parse` (parse only, never compile/exec) decides validity; invalid files get `syntax_error` status and plain-text chunks. tree-sitter extracts units for valid files (exact byte/line positions, decorators included).

### 2026-10-25 · Lines split on LF only
- `str.splitlines()` also splits on form feed, U+2028 etc., which shifted line numbers relative to tree-sitter and `git blame`. All line handling uses `split_lines()` (LF only, strip trailing CR). Tested with form-feed and CRLF sources.

### 2026-10-25 · Clone hardening
- `git` always runs with an argument list, `GIT_TERMINAL_PROMPT=0`, `core.hooksPath=/dev/null`, `core.symlinks=false` (symlinks check out as plain files containing the target path, so nothing can point outside the checkout) and `--` before the clone source. GitHub clones additionally get `protocol.allow=never` + `protocol.https.allow=always` (tested: a `file://` clone fails).
- Size: best-effort pre-clone check via GitHub API `size`, plus a hard on-disk check after clone (directory deleted on violation).
- `.gitignore` is respected by listing files with `git ls-files --cached --others --exclude-standard`; non-git directories fall back to `os.walk` without following links.

### 2026-10-25 · Chunking strategy
- One chunk per top-level function and per method; nested functions stay inside their parent (they rarely make sense alone). Classes get a *summary* chunk with method bodies collapsed to signatures, so class-level questions hit one chunk without duplicating every method. Module-level code outside definitions is chunked as contiguous segments. Oversized units split into overlapping line windows, each with `part i/n of: <signature>` in its header.
- Token counts are estimated as chars/4 (no tokenizer dependency); good enough for size caps.

### 2026-10-25 · Call resolution: what is and isn't resolved
- Resolved: same-module names, enclosing-scope nested defs, imports (aliases, relative, `import a.b`, package re-exports up to 4 hops), `self.m()`/`cls.m()`/`super().m()` via the in-repo class hierarchy, `Class.m()`, `module.f()`, and two cheap local-type rules: `x = Cls(...); x.m()` and annotated parameters `def f(x: Cls): x.m()`, plus `self.attr = Cls()` -> `self.attr.m()`.
- Calls are classified as resolved / unresolved / external (stdlib/third-party, including names re-exported through an in-repo module like `requests.compat.urlparse`) / builtin. **Resolution rate = resolved / (resolved + unresolved)**; builtins and external calls are excluded from the denominator and reported separately, and a non-test rate is reported because pytest fixtures (`httpbin(...)`) are unresolvable by design.
- **psf/requests @ 611c616:** 48.1% before local-type rules -> **58.6% overall, 53.6% non-test** after. Remaining: methods on untyped locals/params (`r.json()`, `sock.send()`), fixtures, dynamic dispatch.

### 2026-10-25 · Incremental indexing re-parses only changed files; graph always rebuilt
- Parse results are cached per file (JSON, keyed by content hash). The graph is rebuilt from all cached parse results on every run: it takes ~0.2 s for requests, and it makes incremental == full by construction (cross-file edges into changed files can't go stale). Tested by mutating the fixture (modify, add, delete, rename) and comparing symbol tables, file hashes and a canonical graph fingerprint against a full index.
- Symbols' primary key is `(repo_id, id)`; ids are uuid5 of `(path, kind:qualname, occurrence)` so they are stable across runs and distinct for `@property`/`@x.setter` pairs.

### 2026-10-25 · PageRank implemented by hand
- `networkx.pagerank` imports scipy; a 20-line power iteration avoids that dependency for the tour's centrality ranking.

---

## Phase 2 — Indexing, retrieval, LLM layer, naive_rag, verifier

### 2026-10-25 · Default model `claude-opus-5-5`; judge `claude-sonnet-5-5`; no `temperature`
- **Why:** the spec leaves the model open; Opus 5.5 is the current default Claude model. The judge is deliberately a *different* model so it doesn't grade its own outputs. Both are env-configurable (`LLM_MODEL`, `JUDGE_MODEL`).
- **Finding:** since 2026-10-21 the Anthropic Messages API rejects `temperature`/`top_p`/`top_k` on every model, and the pinned SDK (`anthropic==1.13.0`) has no `temperature` parameter at all. The spec's "temperature 0 where supported" therefore applies only to OpenAI/Ollama. Run metadata records this explicitly; run-to-run variance must be measured, not assumed away.
- Opus 5.5 specifics handled: `output_config.effort` sent explicitly (its default would be `medium`); forced `tool_choice` (`any`/`tool`) returns 400, so a forced final answer uses `tool_choice: none`; raw assistant content (incl. thinking blocks) is replayed verbatim; server-side refusal `fallbacks: "default"` is enabled and the *served* model is used for cost.

### 2026-10-25 · fastembed models: `jina-embeddings-v2-base-code` (768-d), `Qdrant/bm25`, `jina-reranker-v1-turbo-en`
- All verified in `fastembed==0.9.0`'s supported-model lists before use. CPU-only, no API cost.
- **What didn't work:** model downloads failed behind the TLS-intercepting proxy (`CERTIFICATE_VERIFY_FAILED`: huggingface_hub uses certifi). Fix: `scripts/export_ca_bundle.py` writes certifi + Windows-store roots to a PEM; set `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`. Not needed on normal networks; in Docker, `docker/certs/` handles it.
- The 640 MB code model took 22 min to download on this network; models are cached in `data/models`.

### 2026-10-25 · One Qdrant collection per (repo, chunking strategy)
- **Alternative:** one collection with a `repo_id` payload filter.
- **Why:** deleting or re-indexing a repo is a single `delete_collection`; the chunking ablation doesn't share a collection; payload filters are still used for `kind` (code vs docs). Named vectors `dense` + `sparse` (IDF modifier) in one point; hybrid = two prefetches fused server-side with RRF.

### 2026-10-25 · Incremental vectors tracked by a per-collection manifest, not the SQLite file table
- The structure stage updates SQLite *before* embedding. If embedding then crashed, comparing against SQLite would think those files were done. A manifest (`vectors_<strategy>.json`: models + embedded file hashes) is written only after embedding succeeds; point ids are uuid5 of `(repo, strategy, chunk id)`, so re-running is idempotent. Changing the embedding model resets the collection.
- Tested: modify/add/delete then incremental == full (identical point-id sets); re-indexing an unchanged tree embeds nothing.

### 2026-10-25 · Citation verifier checks existence only; one revision pass
- Citations must point at an *indexed* file with a valid line range; backticked identifiers must be in the symbol table or appear as tokens in the repo (so stdlib calls the repo uses pass). Builtins and keywords are skipped. It does not judge relevance; that is the LLM judge's job, and an honest limitation.

## Phases 3 and 5 — Agents

### 2026-10-25 · Hand-written loop shared by both agents; identical file tools and truncation
- Same `Toolbox` class (argument validation, error capture, output truncation at `TOOL_OUTPUT_MAX_CHARS`), same `read_file` / `list_directory`, same budgets from one `SystemConfig`. A test asserts the resolved budgets and model are equal for both agents.
- **Token budget** = total tokens processed (input + output + cache) summed over calls. Because the transcript is re-sent each step, my first default (60k) would exhaust after ~4 tool calls, so the default is 250k.
- `grep_agent` results are filtered to the indexed file set, so both agents search the same corpus.

### 2026-10-25 · grep: ripgrep when installed, else Python `re` in a killable child process
- ripgrep is not on this Windows machine's PATH (the `rg` in my shell was a tool shim). Python's `re` backtracks catastrophically on patterns like `^(a+)+$` and has no timeout, so it runs in `python -I pygrep.py` and is killed after 10 s (tested). The Docker image installs ripgrep (linear-time engine).

### 2026-10-25 · SQLite stays on the event-loop thread
- **What didn't work:** `sqlite3` connections are thread-bound; calling `mine_commits(conn)` or the grep tool (which reads the file table) via `asyncio.to_thread` raised `ProgrammingError`. Fix: worker threads do pure computation (git, parsing, embedding, regex) and return data; all DB access happens on the loop thread.

## Phase 4 — History

### 2026-10-25 · PyDriller for commit metadata, one `git log --name-only` for changed files
- PyDriller's per-commit `modified_files` costs ~64 ms/commit (19 s for 300 commits of requests, ~7 min for its full history). Metadata traversal is 1.1 s for 6,495 commits; changed files from one `git log` call take ~1 s.

### 2026-10-25 · Pre-link the most recent N commits; PR number from the commit message first
- Squash merges ("Title (#1234)") and merge commits ("Merge pull request #1234") give the PR number without the `commits/{sha}/pulls` call. All GitHub 200/404 responses are cached in SQLite.
- **Observed:** without `GITHUB_TOKEN` (60 req/h) pre-linking stopped after 15–47 commits per repo; the rest link lazily on first `get_commit`. When rate-limited, the tool tells the model to answer from the commit message alone and say so (tested with a mocked 403).

### 2026-10-25 · Migration splitter rewritten (QA-F02 fixed early)
- A `;` inside a `--` comment in migration 0002 split a statement, exactly the audit's QA-F02. Replaced `split(";")` with a small quote- and comment-aware scanner.

## Phase 6 — API

### 2026-10-25 · FastAPI's native SSE (`fastapi.sse.EventSourceResponse`), no extra dependency
- `done` is always the last event; failures become an `error` event with a generic message (no tracebacks). A client disconnect cancels the answering task.
- In-process sliding-window rate limiter and job manager: correct for one process; multi-worker deployments need shared state (e.g. Redis). Documented limitation.
- Webhook: HMAC-SHA256 with constant-time compare, delivery-id replay protection (migration 0003), default branch only, pinned repos ignored, a push during a running job queues one follow-up run.

### 2026-10-25 · CPU work moved off the event loop
- Clone, parse, graph build, embedding and query embedding run in worker threads; otherwise an index job would freeze every API request for minutes.

## Phase 7 — Frontend

### 2026-10-26 · Next.js 16.4.3, not the planned 16.4.0
- `npm audit` reported critical advisories for 16.0.0–16.4.0 (unauthenticated RCE in the image optimizer with SVGs, RCE in `use cache`, a Server Actions DoS). Security fixes override the "at least 2 weeks old" pinning rule.
- TypeScript pinned to 5.9.3: 7.0 (the Go-native compiler) is the latest, but Next's build type-check uses the TypeScript JS API.
- Model output is rendered with react-markdown: raw HTML is never rendered and only `http(s)` links and citation anchors survive `urlTransform`. Shiki output is injected as HTML; Shiki escapes the source text. Playwright tests a `<script>` / `onerror` / `javascript:` payload.
- **What didn't work:** `NEXT_PUBLIC_API_URL` is inlined at build time, so the e2e config builds with the test URL; axe found 4.15:1 contrast on status pills (light theme), fixed by darkening the palette; two copies of `playwright-core` broke `next build`'s type-check until the test files were excluded from the app tsconfig.
- Playwright runs against `backend/tests/e2e_server.py`: the real FastAPI app with in-memory Qdrant, hashing embeddings, the deterministic mock LLM and the fixture repo; no keys or network.

## Phase 8 — Eval harness

### 2026-10-25 · Datasets drafted by Claude sub-agents, validated mechanically, never self-verified
- 3 x 60 questions (12 per category) at pinned commits: requests `611c6162`, flask `d086db85`, fastapi `7a0a3f0b`. `eval/datasets/schema.py` checks gold files exist at the commit, gold symbols are defined in them, `unanswerable` has no gold files, and every `why` reference answer cites a real commit SHA. All are `verified: false` until a human reviews them with `eval/review.py`.
- **What didn't work:** the first three drafting agents stalled after ~10 minutes with nothing written. Re-run with "append each category before starting the next"; all three finished.
- The validator regex initially missed annotated constants (`NAME: T = ...`); fixed.

### 2026-10-27 · Offline mock LLM for pipeline checks
- No API key is configured here, so the 5-question dry run (requests, all three systems) used `--mock-llm`: deterministic "cite the first hit" answers and a heuristic judge. Mock runs are flagged in metadata, excluded from `summary.md` unless `--include-mock`, and watermarked when included. They prove plumbing only.
- Observed in the dry run: `indexed_agent` spends 10–20 s per question even with an instant mock LLM, nearly all of it in the CPU cross-encoder reranking 30 candidates. That is a real latency cost to report.

## Phase 0 audit fixes (after the end-to-end build)

| Finding | Fix |
|---|---|
| QA-F01 migrate TOCTOU | version re-read inside the `BEGIN IMMEDIATE` transaction |
| QA-F01b WAL switch "database is locked" | retry with backoff (a journal-mode change ignores `busy_timeout`) |
| QA-F02 `;` in SQL comment | quote/comment-aware splitter (Phase 4) |
| QA-F03 relative paths depend on the CWD | `DATA_DIR` / `SQLITE_PATH` / `MODELS_CACHE_DIR` resolve against the repo root |
| QA-F04 fixture rationale contradicts the code | message now says "0.5s and then 1s" (2 sleeps for 3 attempts); a test checks it against the decorator |
| QA-F05 hardcoded compose project name | removed; project name = checkout directory. Existing vectors migrated by copying the volume |
| QA-F06 pinning gaps | Qdrant pinned by digest, `.python-version` 3.11.9, uv `required-version`, Dockerfiles pin base images |
| QA-F07 cookie-declared Latin-1 fixture | PEP 263 cookies honoured (`tokenize.detect_encoding`); a cookie-less cp1252 file now exercises replacement |
| QA-F08 `docs/` missing | `docs/architecture.md` (Mermaid) |
| QA-F09 coverage gaps | tests for `get_settings()` caching and the `:memory:` DB |
| QA-F10 non-JSON `CORS_ORIGINS` | JSON list or comma-separated both accepted |
| SEC-01 Qdrant unauthenticated by default | bound to 127.0.0.1; `QDRANT_API_KEY` is passed to both Qdrant and the backend; README says to set it for any deployment |
| QA payload corpora (URLs, traversal, grep injection, ReDoS, 22 hand-counted units) | the audit's pending tests are now wired to the real code and pass. Wiring them found one real bug: a **tracked** file matching `.gitignore` (force-added build output) was indexed. In a fresh clone every file is tracked, so the walker now also drops `git ls-files --cached --ignored --exclude-standard` |
| SEC-02 leakage guard only a comment | settings validator rejects a `DATA_DIR` inside `eval/datasets`; the runner sends systems only the question (tested) |

---

## Follow-up work after the end-to-end build (2026-10-29)

### Token streaming end to end
- **Provider layer:** `chat(..., on_text=callback)`. With a callback the provider streams (`client.messages.stream` / `client.beta.messages.stream` for Anthropic; `stream=True` + `stream_options.include_usage` for OpenAI/Ollama) and still returns the complete turn, so the loop, verifier and cost accounting are unchanged. OpenAI tool calls arrive as JSON fragments keyed by index; they are concatenated and parsed once.
- **Events:** per-token `answer_delta` events go to the live sink only (`Trace.stream`), *not* into the stored trace; otherwise every saved eval run would carry one event per token. A new `turn_end {final}` event is stored: a turn that ends in tool calls was only narration ("let me search..."), so the UI clears that draft; the last turn's text is the answer.
- **UI:** `SystemRun` renders the draft as markdown while it streams (citation chips are live), shows a caret, and replaces it with the verified/possibly-revised result on `result`. The revision pass and the tour/judge calls are not streamed.
- **Test seam:** the scripted and mock providers stream in small chunks; the e2e backend adds a 150 ms delay per word so Playwright can observe a partial draft with no verification badge, then the final answer replacing it.
- Not done: Langfuse still traces one span per answer, not per LLM call/tool step.

### Results page shows the charts
- `GET /results/charts/{name}` serves PNGs written by `eval.summarize`. Names must match `[a-z0-9_]{1,64}\.png` and resolve directly inside the results directory (404 otherwise). The page renders them with descriptive alt text. The e2e backend seeds a real 1x1 PNG and a fixture summary.

### Configurable ignore list
- `EXTRA_IGNORE_DIRS`, `EXTRA_IGNORE_GLOBS`, `UNIGNORE_DIRS` (JSON list or comma-separated) adjust the built-in defaults in `walk.py`. Changing them is picked up by incremental indexing through the normal hash diff (newly ignored files count as removed).

### History pre-linking: most-changed *and* most-recent
- Half of the pre-link budget goes to commits touching the 30 most-changed Python files (ranked by how many mined commits touched each file), half to the most recent; the two groups are interleaved so a rate limit that stops the run early still covered both. Rationale: "why" questions concentrate on code that churns, which pure recency misses.

### ONNX out-of-memory on `fastapi` (found by a real indexing run)
- **What happened:** embedding `fastapi/fastapi` crashed with `BFCArena ... Failed to allocate memory for requested buffer of size 4546167552`. A batch of 16 chunks was padded to its longest chunk (~2,400 tokens) and attention memory grows with batch x length^2.
- **Fix:** texts are sorted by length and batched under an attention-memory budget (so two 6,000-character chunks never share a batch, while short texts still batch 16 wide), and an allocation failure retries with half the batch down to one text. Unit-tested with a fake model that fails on big batches. flask/requests were embedded with the old batching; that affects only speed/memory, not the vectors.
