-- Git history and GitHub PR/issue cache (Phase 4).

CREATE TABLE commits (
    repo_id         TEXT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    sha             TEXT NOT NULL,
    author          TEXT NOT NULL,
    authored_at     TEXT NOT NULL,
    message         TEXT NOT NULL,
    is_merge        INTEGER NOT NULL DEFAULT 0,
    files_json      TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (repo_id, sha)
);
CREATE INDEX idx_commits_date ON commits(repo_id, authored_at);

-- commit -> PR links; `source` says how the link was found
CREATE TABLE commit_prs (
    repo_id         TEXT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    sha             TEXT NOT NULL,
    pr_number       INTEGER NOT NULL,
    source          TEXT NOT NULL CHECK (source IN ('message', 'api')),
    PRIMARY KEY (repo_id, sha, pr_number)
);

-- whether a commit's PR lookup has been attempted, and with what result
CREATE TABLE commit_link_status (
    repo_id         TEXT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    sha             TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('linked', 'no_pr', 'rate_limited', 'error', 'offline')),
    detail          TEXT,
    checked_at      TEXT NOT NULL,
    PRIMARY KEY (repo_id, sha)
);

CREATE TABLE pull_requests (
    repo_id         TEXT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    number          INTEGER NOT NULL,
    title           TEXT NOT NULL,
    body            TEXT,
    url             TEXT NOT NULL,
    state           TEXT,
    merged_at       TEXT,
    review_comments_json TEXT NOT NULL DEFAULT '[]',
    linked_issues_json   TEXT NOT NULL DEFAULT '[]',
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (repo_id, number)
);

CREATE TABLE issues (
    repo_id         TEXT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    number          INTEGER NOT NULL,
    title           TEXT NOT NULL,
    body            TEXT,
    url             TEXT NOT NULL,
    state           TEXT,
    is_pull_request INTEGER NOT NULL DEFAULT 0,
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (repo_id, number)
);

-- raw GitHub API responses, so repeat lookups never hit the API
CREATE TABLE github_cache (
    url             TEXT PRIMARY KEY,
    status          INTEGER NOT NULL,
    body            TEXT NOT NULL,
    fetched_at      TEXT NOT NULL
);
