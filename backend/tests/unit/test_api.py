"""API tests: TestClient runs the real app (lifespan, middleware, background jobs)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.fakes import ScriptedProvider, Step, make_settings, services_for

URL = "https://github.com/acme/shopkit"
REPO_ID = "acme__shopkit"
SECRET = "whsec-test"
ANSWER = "`OrderService.validate_order` raises for empty orders [shop/services.py:20-22]."


def _client(tmp_path: Path, fixture_repo: Path, provider=None, **kw) -> TestClient:
    settings = make_settings(tmp_path, history_prelink_commits=0, **kw)
    app = create_app(settings, lambda s: services_for(s, provider), results_dir=tmp_path / "results",
                     clone_overrides={REPO_ID: str(fixture_repo)})  # fmt: skip
    return TestClient(app)


def _wait_ready(c: TestClient, repo_id: str = REPO_ID, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = c.get(f"/repos/{repo_id}").json()
        if r["status"] in ("ready", "failed"):
            return r
        time.sleep(0.2)
    raise AssertionError("indexing did not finish")


def _assert_error(resp, status: int, code: str) -> None:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == {"error"} and body["error"]["code"] == code and body["error"]["message"]


@pytest.fixture
def ready(tmp_path: Path, fixture_repo: Path):
    provider = ScriptedProvider([Step(calls=[{"name": "search_code", "args": {"query": "empty order"}}]),
                                 Step(ANSWER)])  # fmt: skip
    with _client(tmp_path, fixture_repo, provider, github_webhook_secret=SECRET) as c:
        assert c.post("/repos", json={"url": URL}).status_code == 202
        repo = _wait_ready(c)
        assert repo["status"] == "ready", repo
        yield c, provider


def test_add_repo_lifecycle(ready) -> None:
    c, _ = ready
    repo = c.get(f"/repos/{REPO_ID}").json()
    assert repo["job"]["status"] == "ready" and repo["indexed_sha"]
    assert repo["stats"]["files_python"] == 11 and repo["stats"]["resolution"] > 0
    assert [r["id"] for r in c.get("/repos").json()["repos"]] == [REPO_ID]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://169.254.169.254/",
        "git@github.com:a/b.git",
        "https://github.com.evil.com/a/b",
    ],
)
def test_invalid_urls(tmp_path: Path, fixture_repo: Path, url: str) -> None:
    with _client(tmp_path, fixture_repo) as c:
        _assert_error(c.post("/repos", json={"url": url}), 422, "invalid_repo_url")
        _assert_error(
            c.post("/repos", json={"url": URL, "commit_sha": "--all"}), 422, "invalid_commit_sha"
        )
        _assert_error(c.post("/repos", json={}), 422, "validation_error")
        _assert_error(c.post("/repos", json={"url": 5}), 422, "validation_error")


def test_admin_token_and_rate_limit(tmp_path: Path, fixture_repo: Path) -> None:
    with _client(tmp_path, fixture_repo, admin_token="s3cret", rate_limit_repos_per_minute=2) as c:
        _assert_error(c.post("/repos", json={"url": URL}), 401, "unauthorized")
        _assert_error(c.post("/repos", json={"url": URL}, headers={"Authorization": "Bearer nope"}), 401,
                      "unauthorized")  # fmt: skip
        ok = {"Authorization": "Bearer s3cret"}
        assert c.post("/repos", json={"url": URL}, headers=ok).status_code == 202
        _assert_error(c.post("/repos", json={"url": URL}, headers=ok), 409, "job_running")
        r = c.post("/repos", json={"url": URL}, headers=ok)
        _assert_error(r, 429, "rate_limited")
        assert int(r.headers["retry-after"]) >= 1
        _wait_ready(c)


def test_file_viewer_and_path_safety(ready) -> None:
    c, _ = ready
    r = c.get(f"/repos/{REPO_ID}/file", params={"path": "shop/services.py", "start": 20, "end": 22})
    body = r.json()
    assert r.status_code == 200 and body["start"] == 20 and body["end"] == 22
    assert body["lines"][0].strip().startswith("def validate_order")
    for bad in ("../../etc/passwd", "/etc/passwd", ".git/config", "shop/../../x", "C:\\Windows\\win.ini",
                "%2e%2e/%2e%2e/etc/passwd"):  # fmt: skip
        resp = c.get(f"/repos/{REPO_ID}/file", params={"path": bad})
        assert resp.status_code in (400, 404) and "error" in resp.json(), bad
    _assert_error(
        c.get(f"/repos/{REPO_ID}/file", params={"path": "shop/nope.py"}), 404, "file_not_found"
    )
    _assert_error(c.get("/repos/nope/file", params={"path": "a.py"}), 404, "repo_not_found")
    _assert_error(c.get(f"/repos/{REPO_ID}/file", params={"path": "shop/api.py", "start": 999}), 400,
                  "invalid_range")  # fmt: skip
    _assert_error(c.get("/does-not-exist"), 404, "not_found")


def _sse(c: TestClient, payload: dict) -> list[dict]:
    events = []
    with c.stream("POST", f"/repos/{REPO_ID}/ask", json=payload) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def test_ask_streams_events_then_result(ready) -> None:
    c, _ = ready
    events = _sse(c, {"question": "What if an order is empty?"})
    types = [e["type"] for e in events]
    assert "tool_call" in types and types[-2:] == ["result", "done"]
    assert types.index("turn_end") < types.index("tool_call")  # the search turn ends first
    assert "tool_result_summary" in types and "final" in types
    deltas = [e["text"] for e in events if e["type"] == "answer_delta"]
    assert len(deltas) > 1 and "".join(deltas) == ANSWER  # streamed in pieces, before the result
    assert types.index("answer_delta") < types.index("result")
    result = events[-2]
    assert result["answer"] == ANSWER and result["verification"]["ok"] and "trace" not in result
    assert result["usage"]["tool_calls"] == 1


def test_ask_errors(ready) -> None:
    c, _ = ready
    _assert_error(c.post(f"/repos/{REPO_ID}/ask", json={"question": "q", "system": "bogus"}), 422,
                  "validation_error")  # fmt: skip
    _assert_error(c.post(f"/repos/{REPO_ID}/ask", json={"question": ""}), 422, "validation_error")
    _assert_error(c.post("/repos/nope/ask", json={"question": "q"}), 404, "repo_not_found")
    assert _sse(c, {"question": "first"})[-2]["type"] == "result"  # consumes the script
    # The scripted provider now has no steps left: the stream must still end with error + done.
    events = _sse(c, {"question": "again"})
    assert [e["type"] for e in events][-2:] == ["error", "done"]
    assert "internal error" in events[-2]["message"] and "Traceback" not in json.dumps(events)


def _signed(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _push(ref: str = "refs/heads/main") -> bytes:
    return json.dumps(
        {"ref": ref, "repository": {"full_name": "acme/shopkit", "default_branch": "main"}}
    ).encode()


def test_webhook(ready) -> None:
    c, _ = ready
    body = _push()
    h = {"X-GitHub-Event": "push", "X-GitHub-Delivery": "d-1", "Content-Type": "application/json"}
    _assert_error(c.post("/webhooks/github", content=body, headers=h), 401, "invalid_signature")
    _assert_error(c.post("/webhooks/github", content=body, headers={**h, "X-Hub-Signature-256": _signed(body, "x")}),
                  401, "invalid_signature")  # fmt: skip
    ok = {**h, "X-Hub-Signature-256": _signed(body)}
    r = c.post("/webhooks/github", content=body, headers=ok)
    assert r.status_code == 202 and r.json()["status"] == "reindex_started"
    assert (
        c.post("/webhooks/github", content=body, headers=ok).json()["status"]
        == "duplicate_delivery"
    )
    other = _push("refs/heads/feature")
    r = c.post("/webhooks/github", content=other, headers={**h, "X-GitHub-Delivery": "d-2",
                                                           "X-Hub-Signature-256": _signed(other)})  # fmt: skip
    assert r.json()["status"] == "ignored"
    unknown = json.dumps({"ref": "refs/heads/main", "repository": {"full_name": "x/y"}}).encode()
    _assert_error(c.post("/webhooks/github", content=unknown, headers={**h, "X-GitHub-Delivery": "d-3",
                                                                      "X-Hub-Signature-256": _signed(unknown)}),
                  404, "repo_not_tracked")  # fmt: skip
    assert _wait_ready(c)["status"] == "ready"


def test_webhook_not_configured_and_size_limits(tmp_path: Path, fixture_repo: Path) -> None:
    with _client(tmp_path, fixture_repo, max_webhook_bytes=1000) as c:
        _assert_error(c.post("/webhooks/github", content=b"{}"), 503, "webhook_not_configured")
        _assert_error(c.post("/webhooks/github", content=b"x" * 2000), 413, "payload_too_large")
        _assert_error(c.post("/repos", content=b'{"url": "' + b"a" * 70_000 + b'"}',
                             headers={"Content-Type": "application/json"}), 413, "payload_too_large")  # fmt: skip


def test_cors(tmp_path: Path, fixture_repo: Path) -> None:
    with _client(tmp_path, fixture_repo, cors_origins=["https://app.example"]) as c:
        pre = {"Access-Control-Request-Method": "POST"}
        ok = c.options("/repos", headers={"Origin": "https://app.example", **pre})
        assert ok.headers.get("access-control-allow-origin") == "https://app.example"
        bad = c.options("/repos", headers={"Origin": "https://evil.example", **pre})
        assert "access-control-allow-origin" not in bad.headers


def test_failed_job_reported(tmp_path: Path) -> None:
    with _client(tmp_path, tmp_path / "missing-repo") as c:
        assert c.post("/repos", json={"url": URL}).status_code == 202
        repo = _wait_ready(c)
        assert repo["status"] == "failed" and "Could not clone" in repo["error"]
        assert repo["job"]["status"] == "failed"


def test_result_charts_served_safely(tmp_path: Path, fixture_repo: Path) -> None:
    results = tmp_path / "results"
    results.mkdir()
    png = bytes([0x89]) + b"PNG-not-a-real-image"
    (results / "correctness_vs_cost.png").write_bytes(png)
    (results / "secret.txt").write_text("nope", encoding="utf-8")
    with _client(tmp_path, fixture_repo) as c:
        ok = c.get("/results/charts/correctness_vs_cost.png")
        assert (
            ok.status_code == 200
            and ok.headers["content-type"] == "image/png"
            and ok.content == png
        )
        for bad in ("missing.png", "secret.txt", "..%2Fsecret.png", "a.PNG", "x" * 80 + ".png"):
            r = c.get(f"/results/charts/{bad}")  # a name with '/' never matches the route at all
            assert r.status_code == 404 and r.json()["error"]["code"] in (
                "chart_not_found",
                "not_found",
            ), bad
        _assert_error(c.get("/results"), 404, "no_results")
