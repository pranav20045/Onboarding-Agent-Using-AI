# CodeAtlas: Independent QA & Audit Report

- **Audited commit:** `40aa687` ("Force LF line endings for cross-platform Docker builds"), branch `main`, 5 commits total
- **Date:** 2026-10-25
- **Environment:** Windows 11 Pro, Git Bash, Python 3.11.9, uv 0.13.5 (`python -m uv`), git 2.55.0.windows.5, Docker Desktop, network with TLS interception
- **Paid API spend:** **$0.00.** The codebase contains no LLM-calling code (see §2), so there was nothing to spend on. No API keys are configured on this machine.
- **Scope rule:** I modified no application files. Everything I added lives under `qa/` and `backend/tests/qa/` (see §8).

> **Context:** The project says it has finished **Phase 0 of 10** (setup only), and `PROGRESS.md` says so plainly. Most of TEST_PROMPT.md tests features that don't exist yet: ingestion, agents, API, frontend and eval. Those items are marked **Not found / not implemented**, with grep evidence. I did not invent results for them. Where a check could be prepared now, I built it so it runs as soon as the feature lands (fixtures, payload corpus, tests that skip until then).

> **Note on `BUILD_PROMPT.md`:** During this audit, `git status` started showing `BUILD_PROMPT.md` as modified: one blank line added at the top, with an mtime of 13:58:26. It was clean when the audit started, and I never wrote to it. I left it untouched. The author should check where that change came from.

---

## 1. Executive summary

**Overall readiness: Not ready.** That verdict is for the product, or for showing it in an interview. It is not a criticism of the work done so far. Only Phase 0 exists. None of the three systems, and no API, frontend, eval harness or results exist yet.

**For the Phase 0 foundation itself: Ready with fixes.** Every claim in `PROGRESS.md` about Phase 0 reproduced from a fresh clone:
- The README setup works as written, including the `UV_SYSTEM_CERTS=1` note.
- The 19 tests pass on 3 of 3 runs with no flakes.
- Ruff is clean.
- Qdrant comes up healthy.
- The fixture repo SHAs are deterministic, even with a hostile git environment.

The defects I found are in the SQLite layer and the config. Their impact is low today, but they will bite in Phase 6, when an API process and background index workers share the database.

**Top 5 issues**

| # | ID | Sev | Issue |
|---|---|---|---|
| 1 | QA-F01 | Medium | `migrate()` has a check-then-act race. When several processes call `init_db()` on a new DB, some crash with `table repos already exists`. Reproduced: 15 of 60 processes in one stress run. |
| 2 | QA-F01b | Medium | `connect()` fails with `database is locked` when processes open a brand-new DB at the same time: 7/60, 8/40 and 10/40 processes across runs. The `PRAGMA journal_mode=WAL` switch doesn't wait on `busy_timeout`. |
| 3 | QA-F03 | Low | A relative `DATA_DIR` or `SQLITE_PATH` resolves against the current working directory. That contradicts DECISIONS.md's claim that "backend/ and root invocations behave the same". |
| 4 | QA-F04 | Low | The fixture's flagship "why" commit message says "0.5s/1s/2s backoff", but the code sleeps only twice (0.5s, then 1s). Ground truth for "why" questions contradicts itself. |
| 5 | QA-F05 | Low | `docker-compose.yml` hardcodes `name: codeatlas`. Every checkout shares one compose project and one Qdrant volume, so `docker compose down -v` from a second clone would delete the main dev's vectors. |

---

## 2. Feature verification matrix

### 2a. PROGRESS.md (Phase 0) claims

| Claim | Status | Evidence |
|---|---|---|
| Repo skeleton following §4 | **Partially working** | All `backend/app/{api,core/*,db}`, `tests/{unit,integration,fixtures}` and `eval/*` packages exist. **`docs/` does not exist in a fresh clone**: it is an empty, untracked directory (`git ls-files docs` returns nothing; the clone has no `docs/`). See QA-F08. |
| uv project, Python 3.11.9, uv 0.13.5, direct deps pinned exactly, `uv.lock` committed | **Verified** | `pyproject.toml` uses `==` for all 6 direct deps. `uv sync --locked` succeeds in a fresh clone with a cold cache (19.6 s). |
| Config: pydantic-settings, spec defaults, secrets as `SecretStr` | **Verified** | Defaults 300/6000/1 MB/10/500 confirmed by tests. My test puts 7 secret env vars through `model_dump_json()`, `repr` and `str`: none leak. |
| `.env.example` lists every setting; a test enforces it | **Verified** | `grep -rnE "os\.environ\|getenv" backend/app eval` finds 0 reads outside `Settings`. The only env read in the repo is in the fixture builder, which strips `GIT_*`. The test compares both directions. `docker-compose.yml` also reads `QDRANT_API_KEY`, which is listed. |
| SQLite: stdlib, WAL, FKs, user_version migrations, 0001 schema | **Partially working** | Works for a single process; tests pass. Fails under concurrent first open (QA-F01, QA-F01b), and the statement splitter breaks on `;` inside comments (QA-F02). |
| docker-compose with Qdrant v1.19.2, ports bound to 127.0.0.1, `/healthz` passes | **Verified** | Fresh clone, isolated project: `healthz check passed`, version 1.19.2, `127.0.0.1:6333-6334`. With `QDRANT_API_KEY` set, an unauthenticated `GET /collections` returns **401** and a keyed request returns 200. |
| Ruff (with bandit `S`) clean; `ruff format --check` clean | **Verified** | `All checks passed!`, `24 files already formatted`. I confirmed S602/S605 fire on a `shell=True` / `os.system` sample. |
| Fixture repo: real git, 7 commits, deterministic SHAs, all §7 Phase 0 items | **Verified** (with a content caveat) | HEAD `35b4e2d66547…` from the working tree, from a fresh build, and from a build with `GIT_AUTHOR_NAME=Evil` and `GIT_CONFIG_PARAMETERS='core.autocrlf=true'`: all identical. Every spec item is present. Caveats: QA-F04 (self-contradictory rationale) and QA-F07 (the "non-UTF-8" file declares its encoding). "Identical on every machine" was only tested on one machine. |
| Tests: `uv run pytest` gives 19 passed (config 6, db 7, fixture 6) | **Verified** | 3 runs in a fresh clone: 19/19 each time (10.46 s, 9.75 s, 10.08 s). |
| Known gaps: `core/*`, `api/`, `eval/*` empty; no CI | **Verified (honest)** | All 13 `app/**/__init__.py` files are 0 bytes. There are no `.github/`, Dockerfiles or deploy configs. |

### 2b. README feature list

The README makes no feature claims beyond "early development (Phase 0 of 10)" and the setup steps. **Setup: Verified.** I ran each step from a fresh clone: `cp .env.example .env`, `docker compose up -d`, `cd backend`, `uv sync`, `uv run pytest`. The TLS note is accurate. Without `UV_SYSTEM_CERTS`, `uv sync` fails with `invalid peer certificate: UnknownIssuer` and uv itself suggests `--system-certs`. With it set, the sync succeeds.

README gaps, all minor:
1. It doesn't say the compose project name is fixed (QA-F05).
2. It doesn't mention `ruff` or the fixture-builder command; those are only in PROGRESS.md.
3. It doesn't say `uv` may only be reachable as `python -m uv` on some installs. That is environmental, not a README error.

### 2c. BUILD_PROMPT.md scope items

| Scope item | Status | Evidence (`grep -rniE <pattern> backend/app eval --include=*.py`, excluding config.py) |
|---|---|---|
| Three systems behind a common interface (`answer()` / `SystemAnswer`) | **Not found / not implemented** | `def answer\|SystemAnswer\|SystemConfig`: 0 hits |
| Ablation flags | **Not found** | `max_tool_calls`, `budget_exhausted`, retrieval-mode flags: 0 hits |
| Citation verifier | **Not found** | `verif`: 0 hits |
| Incremental indexing | **Not found** | Only the schema exists (`files.content_hash`, `index_jobs.kind IN ('full','incremental')`). No `qdrant_client` (0 hits). |
| Webhook | **Not found** | `webhook\|X-Hub-Signature\|hmac`: 0 hits (only the `github_webhook_secret` setting) |
| Tour | **Not found** | `tour`: 0 hits (empty `core/tour/__init__.py`) |
| Comparison UI | **Not found** | `git ls-files frontend` lists only `frontend/.gitkeep` |
| Parsing / graph / history / LLM layer | **Not found** | `tree_sitter\|ast\.parse`, `networkx\|pydriller`, `anthropic\|openai\|ollama`: 0 hits |

**Stubs, TODO, fake data:** `grep -rnE "TODO|FIXME|NotImplementedError|^\s*pass\s*$" backend/app eval` finds 0 hits in application code. The 2 `pass` hits are legitimate `except SyntaxError: pass` lines in `tests/unit/test_fixture_repo.py`. I found no hardcoded fake data or mock behaviour in non-test code. Empty packages are disclosed as placeholders, not presented as finished.

---

## 3. Findings (non-security), sorted by severity

All commands run from the repo root unless they say otherwise, with `export UV_SYSTEM_CERTS=1` set.

### QA-F01: `migrate()` reads the schema version outside its transaction (race on first start)
- **Severity:** Medium
- **Area:** `backend/app/db/connection.py` (`migrate`)
- **Reproduction:**
  1. `cd backend && python -m uv run python ../qa/scripts/stress_init_db.py init_db 15`. This starts 4 processes per trial, synchronised by a barrier, all calling `init_db()` on the same new file.
  2. Deterministic version: `python -m uv run pytest tests/qa/test_qa_phase0.py::test_migrate_rechecks_version_inside_transaction --runxfail`
- **Observed:** The first stress run had 26 of 60 processes fail: `15 x OperationalError: table repos already exists` and `11 x database is locked`. The deterministic test fails with `sqlite3.OperationalError: table repos already exists`.
- **Expected:** Concurrent `init_db()` calls are safe. Exactly one process applies the migrations and the others see the new version and skip.
- **Cause:** `version = current_version(conn)` runs before `BEGIN IMMEDIATE`. A second process can apply the migration between that check and the lock.
- **Why it matters:** Phase 6 runs the API and background index jobs together, and uvicorn may run several workers. First start on a fresh volume, which is exactly the demo or deploy path, can crash a worker.
- **Suggested fix:** Inside `transaction(conn)`, re-read `PRAGMA user_version` and skip if it is already at or past `target`. Alternatively, take one `BEGIN IMMEDIATE` around the whole loop. Then remove the `xfail` marker from the QA test.

### QA-F01b: `connect()` raises `database is locked` when processes open a new DB at the same time
- **Severity:** Medium
- **Area:** `backend/app/db/connection.py` (`connect`)
- **Reproduction:** `cd backend && python -m uv run python ../qa/scripts/stress_init_db.py connect 10`
- **Observed:** Failures in every run: `failed_processes=7` (of 60), then `=10` (of 40), all `OperationalError: database is locked`. `connect` does nothing except set pragmas, so the failing statement is the `PRAGMA journal_mode = WAL` switch on a new file. Switching the journal mode needs an exclusive lock and does not reliably honour `busy_timeout`.
- **Expected:** With `busy_timeout = 30000`, concurrent opens wait instead of failing. DECISIONS.md justifies WAL with "concurrent readers during background indexing".
- **Suggested fix:** Set WAL once, in the single migrating process, inside a retry loop. Or catch `database is locked` on the pragma and retry with backoff, since the mode persists in the file once it is set. The test is timing-dependent, so it lives as a script rather than a pytest assertion.

### QA-F02: A `;` inside a `--` SQL comment breaks a migration
- **Severity:** Low (latent; the current `0001_initial.sql` has no such comment)
- **Area:** `connection._split_statements`
- **Reproduction:** `cd backend && python -m uv run pytest tests/qa/test_qa_phase0.py::test_migration_with_semicolon_in_comment --runxfail`. Equivalent snippet: `_split_statements("-- add table a; and b\nCREATE TABLE a (x INTEGER);")`.
- **Observed:** The output is `['and b\nCREATE TABLE a (x INTEGER)', ...]`, then `sqlite3.OperationalError: near "and": syntax error`. The SQL is split on `;` before comment lines are removed.
- **Expected:** Comments are stripped first. At minimum, DECISIONS.md should document the limitation. It currently says only "must not contain `;` inside string literals or triggers", so a natural English comment like "repos; jobs" would break the next migration.
- **Suggested fix:** Strip `--` comment lines before splitting, or use `sqlite3.complete_statement()` to accumulate statements. Also add the case to the DECISIONS limitation.

### QA-F03: A relative `DATA_DIR` or `SQLITE_PATH` depends on the current working directory
- **Severity:** Low
- **Area:** `backend/app/config.py`
- **Reproduction:** `cd backend && python -m uv run pytest tests/qa/test_qa_phase0.py -k "relative" --runxfail`. Probe output:
  ```
  cwd=repo root -> data_dir D:\Genai pr1\data
  cwd=backend/  -> data_dir D:\Genai pr1\backend\data
  sqlite_path="./x.db" -> db_path x.db (is_absolute False)
  ```
- **Observed:** `_resolve_data_dir` calls `.resolve()`, which resolves against the CWD. `sqlite_path` is never resolved at all.
- **Expected:** DECISIONS.md says "`.env` is resolved relative to the repo root, not the CWD, so `backend/` and root invocations behave the same", and `.env.example` says "default: ./data at the repo root". A user who writes `DATA_DIR=./data` in `.env` gets two different databases depending on where they ran the command.
- **Suggested fix:** Resolve relative paths against `REPO_ROOT` (`(REPO_ROOT / v).resolve()` when `not v.is_absolute()`), and do the same for `sqlite_path`.

### QA-F04: The fixture's design-rationale commit contradicts the code it describes
- **Severity:** Low (affects eval and test ground truth)
- **Area:** `backend/tests/fixtures/build_fixture_repo.py`
- **Reproduction:** Read `UTILS_V1.retry` and `PAYMENTS_V2` (`times=3, backoff=0.5`), then compare with `DESIGN_COMMIT_BODY`. I reasoned this from the source and did not execute the fixture code. The wrapper sleeps only between attempts (`if attempt == times - 1: raise` comes before `time.sleep`), so with 3 attempts it sleeps twice: 0.5 s, then 1.0 s.
- **Observed:** The commit message says "Three attempts with 0.5s/1s/2s backoff". That is three delays, but the code only has two.
- **Expected:** The fixture is "the ground truth for most later tests" and the flagship "why does this retry 3 times?" example. Its rationale should match the code. Otherwise an agent that reads the code correctly will "contradict" the history, and grading becomes ambiguous.
- **Suggested fix:** Change the message to "0.5s/1s backoff (two waits)". If the mismatch is meant as a trap, document that in the fixture's docstring.

### QA-F05: The compose project name is hardcoded, so all checkouts share one container and volume
- **Severity:** Low
- **Area:** `docker-compose.yml` (`name: codeatlas`)
- **Reproduction:** In a fresh clone, run `docker compose config | grep -E "^name|name: codeatlas"` and `docker compose ls`.
- **Observed:** The clone resolves to project `codeatlas`, volume `codeatlas_qdrant_storage` and network `codeatlas_default`, the same ones the existing project bound to `D:\Genai pr1\docker-compose.yml` uses. I inferred the behaviour from the resolved config. I did **not** run `up` or `down -v` under the shared name, to avoid touching the running container. For the reproducibility check I used `-p codeatlasqa`, then restored the original container.
- **Expected:** A second checkout (QA clone, worktree, CI) is isolated. In practice, `docker compose down -v` in a scratch clone would delete the main checkout's indexed vectors.
- **Suggested fix:** Drop `name:` so it defaults to the directory name, or document the shared name and recommend `-p`.

### QA-F06: Reproducibility pinning gaps
- **Severity:** Low
- **Area:** Infra
- **Evidence:**
  - Qdrant is pinned by a mutable tag. `docker image inspect` gives digest `sha256:b7b0444c4c351c970b98e90a6f89c2ee4287c65b44e52b4cb503fa5b2aa927ad`.
  - There is no `[tool.uv] required-version`.
  - `.python-version` is `3.11`, not a patch version.
  - There are no Dockerfiles yet, so "Docker images build without cache" is **not applicable / not found**. `docker pull qdrant/qdrant:v1.19.2` from the registry succeeds.
- **Suggested fix:** Pin `qdrant/qdrant:v1.19.2@sha256:b7b0444c…`, add `required-version = "==0.13.5"` (or a range), and add Dockerfiles with pinned base digests in Phase 6/10.

### QA-F07: The fixture's "non-UTF-8 file" declares its encoding, so it doesn't test decode-with-replacement
- **Severity:** Low
- **Area:** Fixture coverage
- **Evidence:** `shop/locale_fr.py` starts with `# -*- coding: latin-1 -*-`. A PEP 263-aware decoder handles it correctly and never hits the "decode with replacement, log a warning" path from BUILD_PROMPT §5.1.
- **Fix:** Added. The QA fixture `pkg/cp1252_no_cookie.py` is cp1252 bytes with no cookie. Use both cases in Phase 1 tests.

### QA-F08: `docs/` is missing from a fresh clone
- **Severity:** Low (informational)
- **Evidence:** `git ls-files docs` returns nothing. After `git clone`, `ls` shows no `docs/`. PROGRESS lists `docs/` as part of the done skeleton.
- **Fix:** Commit `docs/.gitkeep`, or the Mermaid architecture placeholder.

### QA-F09: Coverage gaps in otherwise well-covered code
- **Severity:** Low (informational)
- **Evidence:** See §6. `config.get_settings()` (line 96, the cached accessor the app will actually use) is never called by a test. In `connect`, the `":memory:"` branches 22→24 and 28→30 are partial.
- **Fix:** Add one test that calls `get_settings()` twice and checks the cache, and one for `connect(":memory:")`.

### QA-F10: A non-JSON `CORS_ORIGINS` gives a raw `SettingsError` at startup
- **Severity:** Low (informational)
- **Reproduction:** `CORS_ORIGINS=http://localhost:3000` → `SettingsError error parsing value for field "cors_origins" from source "EnvSettingsSource"`.
- **Note:** `.env.example` documents the JSON-list format, so this is acceptable. A comma-separated fallback validator would make it friendlier for deployers on Render or Railway, who often type plain URLs.

---

## 4. Security findings

All TEST_PROMPT §4 items are checked below. Most of the attack surface doesn't exist yet.

| Item | Status | Evidence |
|---|---|---|
| Repo URL validation / SSRF | **Not found / not implemented** | `grep -rniE "validate.*url\|github\.com" backend/app`: 0 hits. **Prepared:** 33 reject and 3 accept payloads in `qa/fixtures/security_payloads.json` (`file://`, localhost, 169.254.169.254, IPv4/IPv6, ssh, `git@`, credentials, `github.com.evil.com`, `github.com@evil.com`, encoded `..`, NUL, option injection `--upload-pack`, newline, overlong). `test_qa_security_pending.py` runs them automatically once `app.core.ingest` exposes `validate_repo_url` / `parse_github_url` / `validate_github_url`. |
| Path traversal (`/file`, agent tools) | **Not found** | No API, no file tools. A 14-payload corpus is prepared, including symlink escapes that exist as real git symlinks in `qa/fixtures/.build/edge_parse` and an `eval/datasets/...` leakage probe. |
| Command injection in grep/git tools | **Not found** (tools absent). Positive check: **Verified.** | No `shell=True`, `os.system` or `os.popen` anywhere in `backend/`; my test `test_no_shell_true_in_application_code` enforces this. Both `subprocess.run` calls (fixture builder, tests) pass argument lists. Ruff S602/S605 are active (I verified they fire). |
| ReDoS | **Not found** | 4 pathological patterns are prepared in the corpus. |
| Webhook (signature, replay, wrong repo, size) | **Not found** | Only the `GITHUB_WEBHOOK_SECRET` setting exists, typed as `SecretStr`. |
| Resource limits / rate limiting / body size | **Not found** | Only the size-limit settings exist. Nothing enforces them. |
| Secrets in logs, errors, SSE, traces, bundle, git history | **Verified for what exists** | `git log -p --all` grepped for Anthropic, OpenAI, GitHub, AWS, private-key and Langfuse patterns: the only hit is the dummy `sk-ant-supersecret` in `test_config.py`, which is not a real key. No `.env`, `.db` or `.pem` files are tracked. `SecretStr` holds up under `repr`, `str` and `model_dump_json` (my test covers 7 secrets). There are no logs, SSE or frontend yet. |
| CORS / `ADMIN_TOKEN` | **Not found** | Settings exist (`cors_origins` default `["http://localhost:3000"]`, `admin_token: SecretStr`). No middleware or enforcement. |
| Repo code execution | **Verified (code review)** | No `import`, `exec`, `eval`, `pip`, `subprocess` or `importlib` of repo content in `backend/app`. Fixture tests only call `ast.parse` and `read_text`. My own QA tests also never import fixture code. |

### SEC-01: Qdrant runs without authentication by default
- **Severity:** Low today. **High** if the ports are ever published beyond loopback, for example in the Phase 10 deploy.
- **Reproduction:** `cp .env.example .env && docker compose -p codeatlasqa up -d && curl -s http://127.0.0.1:6333/collections`
- **Observed:** `{"result":{"collections":[]},"status":"ok"}`, with no credentials. The empty `QDRANT_API_KEY` is passed through as `QDRANT__SERVICE__API_KEY: ""`. With a key set, I confirmed the unauthenticated response is `401 Must provide an API key`.
- **Expected:** This is acceptable on a dev machine because the ports are bound to `127.0.0.1`. For a deploy, an empty key should be refused.
- **Suggested fix:** In the production compose or deploy guide, require `QDRANT_API_KEY` (`${QDRANT_API_KEY:?must be set}`), and keep the loopback binding.

### SEC-02: The eval-leakage guard is only a comment
- **Severity:** Low (latent; it matters from Phase 2/8 on)
- **Evidence:** In `config.py`, `data_dir` says `description="... Never inside eval/datasets."`, but `_resolve_data_dir` only does `expanduser().resolve()`. Nothing rejects `DATA_DIR=eval/datasets` or a parent of it, and there is no ingest-side exclusion yet.
- **Suggested fix:** Add a validator that rejects a `data_dir` equal to, inside, or a parent of `REPO_ROOT/eval/datasets`. Phase 3/5 path-safety tests should also include `eval/datasets/*` (the corpus already does).

### SEC-03: S603 is suppressed project-wide (review note)
- **Severity:** Informational
- **Evidence:** `pyproject.toml` ignores S603 and S607, so list-argument `subprocess` calls with untrusted arguments are never flagged. List arguments prevent shell injection, but not **git/rg option injection** (`--upload-pack=`, `--pre=`, `--output=`).
- **Suggested fix:** When the Phase 4/5 git and grep tools land, always put `--` before user-supplied patterns and paths, reject arguments that start with `-`, and run the prepared `grep_pattern_injection` corpus.

---

## 5. Evaluation integrity audit (TEST_PROMPT §8)

**Not found / not implemented.** There are no evaluation results, so no README numbers can be audited.

| Check | Status | Evidence |
|---|---|---|
| Leakage: `eval/datasets/` never indexed or readable | **Not testable**; guard not enforced (SEC-02) | `git ls-files eval` lists only `__init__.py` files and `.gitkeep`s. There is no indexer or agent tool to probe. |
| Pinned commits for eval | **Not found** | No datasets, so no `commit_sha` values. The `repos.pinned_sha` column exists in the schema. |
| Verified-question counts per repo and category | **0 questions** | `eval/datasets/` holds only `.gitkeep`. |
| Metrics correctness (recall@k, hit rate, refusal, p50/p95, cost) | **Not found** | `grep -rniE "recall\|kappa\|judge\|summary\.md" eval backend/app`: 0 hits. Hand-computed check cases were not prepared because there is no function signature to target. |
| Judge model fixed and separate; calibration agreement | **Not found** | No judge code and no calibration files. No human grades exist. |
| README numbers trace to `eval/results/` with config, model and SHA | **Nothing to trace** | `eval/results/` holds only `.gitkeep`. The README contains **no numbers and no results claims**, which is correct for this phase. |
| Fair comparison (same model and budgets) | **Not found** | Only shared settings exist (`AGENT_MAX_TOOL_CALLS`, `AGENT_MAX_TOKENS`, `TOOL_OUTPUT_MAX_CHARS`, `LLM_MODEL`). Having one shared budget setting is a good sign for fairness later. |
| README analysis claims | **None made** | The README states the research question only. |

**Forward-looking risk worth recording now:**
- BUILD_PROMPT §6.2 requires "temperature (0 where supported)". According to the Anthropic API reference bundled with Claude Code, since 2026-10-21 any Messages request that sends `temperature`, `top_p` or `top_k` returns a 400 on every Claude model, including the configured default `claude-sonnet-5-5`, which is a valid model ID. The run metadata should record "temperature: not supported (provider default)" rather than claim 0. The fairness argument should rest on the same model, budgets and question set, plus repeated runs to measure variance.
- I couldn't independently confirm the price table for the cost metric, because it doesn't exist yet. Per that same reference: Sonnet 5.5 costs $2 in / $10 out per MTok.

---

## 6. Performance numbers

| Metric | Value | How measured |
|---|---|---|
| Fresh `uv sync --locked` (cold uv cache, TLS-intercepted network) | 19.6 s | `UV_CACHE_DIR=<empty dir> time python -m uv sync --locked` in a fresh clone |
| Existing test suite (19 tests), 3 runs | 10.46 s / 9.75 s / 10.08 s | `uv run --locked pytest -q -p no:cacheprovider` in a fresh clone |
| Suite with coverage | 24.76 s | `uv run --locked --with pytest-cov pytest --cov=app --cov-branch` |
| Combined suite with QA tests (working tree), 3 runs | 8.14 s / 8.36 s / 7.96 s | Same command, working tree |
| Fixture repo build (7 commits) | about 2 s | `--durations`: `test_build_is_deterministic` 2.2–2.3 s |
| Qdrant idle | 41.3 MiB RAM, 0.19 % CPU; image 74.8 MB | `docker stats --no-stream`, `docker image inspect` |
| Index time / peak memory, latency p50/p95, tokens, cost per question | **Not found / not implemented** | No indexer or systems exist |
| Deployed cold start | **Not found** | No deployment exists |

**Coverage (`--cov=app --cov-branch`):**

| Module | Stmts | Miss | Branch | Partial | Cover |
|---|---|---|---|---|---|
| `app/config.py` | 50 | 1 (line 96, `get_settings`) | 0 | 0 | 98 % |
| `app/db/connection.py` | 65 | 0 | 20 | 2 (`:memory:` branches) | 98 % |
| **`app/core/**` (all 10 packages)** | **0** | – | – | – | **n/a: no code** |
| TOTAL | 115 | 1 | 20 | 2 | 98 % |

The 98 % figure covers only config and DB code. **`backend/app/core/` has zero statements**, so every core module currently has no tests, because none of them exist yet.

---

## 7. Interview risk list

| # | Risk an interviewer could poke at | How to fix or frame it honestly |
|---|---|---|
| 1 | "Your DB layer uses WAL for concurrency. What happens when two workers start?" It crashes (QA-F01/F01b). | Fix both before Phase 6 and keep `qa/scripts/stress_init_db.py` as a regression check. That turns the bug into a strong story: "found a TOCTOU in my migration runner with a multiprocess stress test." |
| 2 | "Your why-question fixture says 0.5/1/2 s, but the code waits twice." (QA-F04) | Fix the message, or call the mismatch a deliberate "history vs code disagreement" test case and grade for it. |
| 3 | "How is `temperature=0` enforced for fairness?" The current API rejects sampling parameters. | Record it in DECISIONS: same model, budgets and question set, N repeated runs, and report the variance. Don't claim temperature 0. |
| 4 | "You say eval datasets can never be indexed. Where is that enforced?" Only in a docstring (SEC-02). | Add the validator and a path-safety test that targets `eval/datasets/`. |
| 5 | "Tested deterministic on every machine?" It was tested twice on one machine. | Say "hermetic by construction (no global/system config, fixed dates and author), verified on Windows". It also held under a hostile `GIT_CONFIG_PARAMETERS` in this audit. Adding Linux CI would back the claim. |
| 6 | "98 % coverage" | Phrase it as "98 % of config and DB". `core/` has no code yet. Don't quote the total without that context. |
| 7 | "Why is `name: codeatlas` hardcoded in compose?" (QA-F05) | Remove it or justify it. Show that you know `down -v` is destructive. |
| 8 | "Is Qdrant authenticated?" Not by default (SEC-01). | Point to the 127.0.0.1 binding for dev, and make the key required in the deploy compose. |
| 9 | "Why ignore S603 globally?" | Good answer: list arguments avoid shell injection. Then show the `--` separator and option-injection tests for git and rg. |
| 10 | No CI. | Add a GitHub Actions workflow on ubuntu and windows: `uv sync --locked`, `ruff`, `pytest`. That also backs the "every machine" claim. |
| 11 | Process: PROGRESS and DECISIONS are honest and match reality. This is a strength. | Keep it that way. The audit found no overclaiming in Phase 0 docs, apart from the CWD statement in DECISIONS (QA-F03) and the incomplete `;` limitation (QA-F02). |

---

## 8. New tests added

| File | What it does | Status today |
|---|---|---|
| `backend/tests/qa/__init__.py` | Package marker so pytest's import mode resolves names correctly | – |
| `backend/tests/qa/test_qa_phase0.py` | 6 passing checks: `.env.example` loads verbatim; no secret leak in JSON, repr or str; no key patterns in `git log -p --all`; no tracked `.env`, `.db` or `.pem`; no `shell=True` or `os.system` in `app/`; the stress script exists. 4 **`xfail(strict=True)`** reproductions: QA-F01 (deterministic TOCTOU), QA-F02, QA-F03 (×2). When a bug is fixed its test XPASSes, strict mode fails the run, and you remove the marker. | 6 pass, 4 xfail |
| `backend/tests/qa/test_qa_fixtures.py` | Builds the QA fixture repos and checks their shape (git symlink mode 120000, force-added gitignored file, >1 MB file, non-UTF-8, binary, Python 2 and syntax-error files, empty repo, no-Python repo). Cross-checks 22 hand-counted units (kind, qualified name, start and end line, decorators included) against `ast`. Two Phase 1 checks skip until a parser or walker exists. | 3 pass, 2 skip |
| `backend/tests/qa/test_qa_security_pending.py` | Runs the URL payload corpus (36 cases) automatically once a validator exists. Placeholders for path safety, grep and webhook. | 39 skip |
| `qa/fixtures/build_qa_fixtures.py` | Hermetic builder for `edge_parse`, `empty_repo` and `no_python` (TEST_PROMPT §3). It covers decorators, nested functions and classes, async, lambda, `@property`, `@staticmethod`/`@classmethod`, dataclass, a 600-line function, unicode identifiers, syntax errors, Python 2, an empty file, cp1252 without a cookie, a binary `.py`, a 1.53 MB file, 40-level nesting (single-character directory names to stay under Windows MAX_PATH), symlinks escaping the repo (relative and absolute), and vendored `site-packages`, `node_modules` and `build/` dirs. | – |
| `qa/fixtures/.gitignore` | Ignores `qa/fixtures/.build/` (nested git repos) | – |
| `qa/fixtures/security_payloads.json` | Corpus: 33 bad and 3 good repo URLs, 14 path-traversal, 7 grep-injection and 4 ReDoS payloads | – |
| `qa/scripts/stress_init_db.py` | Multiprocess reproduction of QA-F01/F01b. Exits 1 if any process fails. | Fails today (as intended) |

**How to run** (from `backend/`, with `UV_SYSTEM_CERTS=1` behind TLS interception):
```bash
uv run pytest tests/qa -rxXs                        # QA tests only (part of the default `uv run pytest` too)
uv run pytest tests/qa --runxfail                   # show the 4 reproduced defects as real failures
uv run python ../qa/scripts/stress_init_db.py init_db 15
uv run python ../qa/scripts/stress_init_db.py connect 15
uv run python ../qa/fixtures/build_qa_fixtures.py   # builds into qa/fixtures/.build/
```
Full suite with QA tests, 3 runs: **28 passed, 41 skipped, 4 xfailed** each time. Ruff check and format are clean on all new files.

**Not checked, and why:**
- §5–§10 subjects don't exist yet: agents, indexing, API, eval, frontend, performance under load.
- The Playwright and axe checks need a frontend.
- No real-LLM tests, because there is no LLM code; $0 spent.
- The interim pauses after §1/§4/§8 were not possible because this was a non-interactive run.
- Cross-OS fixture determinism: only Windows was available.
