-- Core metadata: repositories, background index jobs, per-file hashes and the symbol table.
-- History cache tables (commits, PRs, issues) are added in a later migration (Phase 4).

CREATE TABLE repos (
    id              TEXT PRIMARY KEY,
    url             TEXT NOT NULL UNIQUE,
    owner           TEXT NOT NULL,
    name            TEXT NOT NULL,
    branch          TEXT,
    pinned_sha      TEXT,
    indexed_sha     TEXT,
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'indexing', 'ready', 'failed')),
    error           TEXT,
    stats_json      TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE index_jobs (
    id              TEXT PRIMARY KEY,
    repo_id         TEXT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CHECK (kind IN ('full', 'incremental')),
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'cloning', 'parsing', 'graph', 'history',
                                      'embedding', 'ready', 'failed')),
    progress_done   INTEGER NOT NULL DEFAULT 0,
    progress_total  INTEGER NOT NULL DEFAULT 0,
    target_sha      TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    started_at      TEXT,
    finished_at     TEXT
);
CREATE INDEX idx_index_jobs_repo ON index_jobs(repo_id, created_at);

CREATE TABLE files (
    repo_id         TEXT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    path            TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    language        TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    parse_status    TEXT NOT NULL DEFAULT 'ok'
                    CHECK (parse_status IN ('ok', 'syntax_error', 'decode_replaced', 'text_only')),
    warning         TEXT,
    PRIMARY KEY (repo_id, path)
);

CREATE TABLE symbols (
    id              TEXT NOT NULL,
    repo_id         TEXT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    path            TEXT NOT NULL,
    kind            TEXT NOT NULL
                    CHECK (kind IN ('module', 'class', 'function', 'method')),
    name            TEXT NOT NULL,
    qualified_name  TEXT NOT NULL,
    parent          TEXT,
    start_line      INTEGER NOT NULL CHECK (start_line >= 1),
    end_line        INTEGER NOT NULL CHECK (end_line >= start_line),
    signature       TEXT,
    docstring       TEXT,
    decorators_json TEXT NOT NULL DEFAULT '[]',
    content_hash    TEXT NOT NULL,
    PRIMARY KEY (repo_id, id),
    FOREIGN KEY (repo_id, path) REFERENCES files(repo_id, path) ON DELETE CASCADE
);
CREATE INDEX idx_symbols_repo_qname ON symbols(repo_id, qualified_name);
CREATE INDEX idx_symbols_repo_name ON symbols(repo_id, name);
CREATE INDEX idx_symbols_repo_path ON symbols(repo_id, path);
