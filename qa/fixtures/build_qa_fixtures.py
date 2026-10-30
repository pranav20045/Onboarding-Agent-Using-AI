"""Build QA edge-case fixture repos (TEST_PROMPT.md section 3) as real git repos.

Independent of the project's own `backend/tests/fixtures/build_fixture_repo.py`.
Everything generated here is DATA: never import, execute or install it.

Repos built under DEST (default: qa/fixtures/.build/):

* ``edge_parse``  - parsing edge cases: decorators, nested defs/classes, async, lambdas,
  @property, @staticmethod/@classmethod, dataclasses, a very long function, unicode
  identifiers, syntax error, Python 2, empty file, non-UTF-8 without coding cookie, binary
  bytes with a .py extension, a >1 MB file, 40-level deep directories, a force-added file
  matching .gitignore, vendored site-packages / node_modules / venv dirs, and symlinks that
  point outside the repo (relative and absolute). Symlinks are written straight into the git
  index (mode 120000), so they exist in the repo on every OS without needing privileges.
* ``empty_repo``  - ``git init`` with no commits.
* ``no_python``   - Markdown/YAML/TOML only.

``EXPECTED_UNITS`` is hand-written ground truth for ``pkg/units.py`` (kind, qualified name,
start_line, end_line, 1-based inclusive, decorators included in the span as tree-sitter's
``decorated_definition`` does). ``backend/tests/qa/test_qa_fixtures.py`` cross-checks it
against Python's ``ast`` so the ground truth itself is verified.

Usage:  python qa/fixtures/build_qa_fixtures.py [DEST]
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_DEST = Path(__file__).parent / ".build"

UNITS_PY = '''\
"""Unit extraction edge cases. Line numbers matter: see EXPECTED_UNITS."""

import functools
from dataclasses import dataclass, field


def plain(a, b):
    """Plain function."""
    return a + b


@functools.lru_cache(maxsize=None)
def decorated(n):
    return n * 2


async def fetch(url):
    return url


def outer():
    def inner():
        def innermost():
            return 3

        return innermost()

    class LocalClass:
        def local_method(self):
            return 1

    return inner()


square = lambda x: x * x  # noqa: E731


class Shape:
    """Base shape."""

    sides = 0

    def __init__(self, name):
        self.name = name

    @property
    def label(self):
        return self.name.upper()

    @staticmethod
    def unit():
        return Shape("unit")

    @classmethod
    def create(cls, name):
        return cls(name)

    async def refresh(self):
        return self

    class Meta:
        ordering = ["name"]

        def describe(self):
            return "meta"


@dataclass
class Point:
    x: int
    y: int = 0
    tags: list = field(default_factory=list)

    def norm2(self):
        return self.x * self.x + self.y * self.y


def größe(wert):
    return wert


class Café:
    def crème(self):
        return "brûlée"


@functools.wraps(plain)
@functools.lru_cache(maxsize=1)
def multi_decorated():
    return None
'''

# (kind, qualified name relative to module pkg.units, start_line, end_line)
EXPECTED_UNITS: list[tuple[str, str, int, int]] = [
    ("function", "plain", 7, 9),
    ("function", "decorated", 12, 14),
    ("function", "fetch", 17, 18),
    ("function", "outer", 21, 32),
    ("function", "outer.inner", 22, 26),
    ("function", "outer.inner.innermost", 23, 24),
    ("class", "outer.LocalClass", 28, 30),
    ("method", "outer.LocalClass.local_method", 29, 30),
    ("class", "Shape", 38, 65),
    ("method", "Shape.__init__", 43, 44),
    ("method", "Shape.label", 46, 48),
    ("method", "Shape.unit", 50, 52),
    ("method", "Shape.create", 54, 56),
    ("method", "Shape.refresh", 58, 59),
    ("class", "Shape.Meta", 61, 65),
    ("method", "Shape.Meta.describe", 64, 65),
    ("class", "Point", 68, 75),
    ("method", "Point.norm2", 74, 75),
    ("function", "größe", 78, 79),
    ("class", "Café", 82, 84),
    ("method", "Café.crème", 83, 84),
    ("function", "multi_decorated", 87, 90),
]
# A lambda bound to a name is not a def; record where it is so parsers can be checked
# for consistent handling (either ignored or reported as `square`).
LAMBDA_LINE = 35

LONG_FUNCTION_BODY_LINES = 600


def _long_function() -> str:
    body = "".join(f"    total += {i}  # step {i}\n" for i in range(LONG_FUNCTION_BODY_LINES))
    return (
        f'def very_long():\n    """Needs sub-chunking."""\n    total = 0\n{body}    return total\n'
    )


EDGE_FILES: dict[str, bytes] = {
    "README.md": b"# edge_parse\n\nQA fixture. Data only.\n",
    ".gitignore": b"generated/\n*.log\n",
    "pkg/__init__.py": b"",
    "pkg/units.py": UNITS_PY.encode("utf-8"),
    "pkg/long_function.py": _long_function().encode("utf-8"),
    "pkg/empty.py": b"",
    "pkg/syntax_error.py": b"def broken(:\n    return 1\n",
    "pkg/py2_only.py": b'print "hello"\nexec "x = 1"\n',
    # cp1252 bytes, no PEP 263 cookie -> not decodable as UTF-8
    "pkg/cp1252_no_cookie.py": "LABEL = 'naïve – “quoted”'\n".encode("cp1252"),  # noqa: RUF001  (deliberate cp1252-only chars)
    "pkg/binary_blob.py": bytes(range(256)) * 8,
    # > 1 MB default MAX_FILE_SIZE_BYTES
    "pkg/huge_generated.py": b"".join(b"VALUE_%07d = %d\n" % (i, i) for i in range(70_000)),
    "generated/ignored_build.py": b"def should_be_skipped_gitignored():\n    return 1\n",
    "venv/lib/python3.11/site-packages/vendored_lib/__init__.py": (
        b"def should_be_skipped_site_packages():\n    return 1\n"
    ),
    "node_modules/some_pkg/helper.py": b"def should_be_skipped_node_modules():\n    return 1\n",
    "build/lib/pkg/units.py": b"def should_be_skipped_build_dir():\n    return 1\n",
}
DEEP_DIR_LEVELS = 40
DEEP_FILE = "/".join(["n"] * DEEP_DIR_LEVELS) + "/deep_module.py"  # short names: Windows MAX_PATH
EDGE_FILES[DEEP_FILE] = b"def deeply_nested():\n    return 40\n"

# path in repo -> symlink target (stored as a git symlink, mode 120000)
SYMLINKS: dict[str, str] = {
    "pkg/escape_relative.py": "../../outside_secret.py",
    "pkg/escape_absolute": "/etc/passwd",
    "pkg/inside_link.py": "units.py",  # legit in-repo symlink
}

NO_PYTHON_FILES: dict[str, bytes] = {
    "README.md": b"# no_python\n\nOnly docs and config.\n\n## Setup\n\nNothing to run.\n",
    "config/app.yaml": b"name: demo\nport: 8080\n",
    "pyproject.toml": b'[project]\nname = "no-python"\nversion = "0.0.0"\n',
}


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="QA Bot",
        GIT_AUTHOR_EMAIL="qa-bot@example.com",
        GIT_COMMITTER_NAME="QA Bot",
        GIT_COMMITTER_EMAIL="qa-bot@example.com",
        GIT_AUTHOR_DATE="1704103200 +0000",
        GIT_COMMITTER_DATE="1704103200 +0000",
    )
    return env


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> str:
    out = subprocess.run(
        ["git", *args], cwd=repo, env=_env(), check=True, capture_output=True, input=stdin
    )
    return out.stdout.decode("utf-8", "replace").strip()


def _fresh(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path, onerror=lambda f, p, _e: (os.chmod(p, 0o700), f(p)))
    path.mkdir(parents=True)
    _git(path, "init", "--quiet", "--initial-branch=main")
    return path


def _commit_files(repo: Path, files: dict[str, bytes], message: str) -> None:
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        _git(repo, "add", "--force", "--", rel)  # --force: keep the .gitignored file too
    _git(repo, "commit", "--quiet", "-m", message)


def build_edge_parse(dest: Path) -> Path:
    repo = _fresh(dest / "edge_parse")
    _commit_files(repo, EDGE_FILES, "Add parsing edge cases")
    for rel, target in SYMLINKS.items():
        blob = _git(repo, "hash-object", "-w", "--stdin", stdin=target.encode())
        _git(repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},{rel}")
    _git(repo, "commit", "--quiet", "-m", "Add symlinks (two escape the repo)")
    return repo


def build_empty_repo(dest: Path) -> Path:
    return _fresh(dest / "empty_repo")


def build_no_python(dest: Path) -> Path:
    repo = _fresh(dest / "no_python")
    _commit_files(repo, NO_PYTHON_FILES, "Docs and config only")
    return repo


def build_all(dest: Path = DEFAULT_DEST) -> dict[str, Path]:
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    return {
        "edge_parse": build_edge_parse(dest),
        "empty_repo": build_empty_repo(dest),
        "no_python": build_no_python(dest),
    }


if __name__ == "__main__":
    built = build_all(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DEST)
    for name, path in built.items():
        print(f"{name}: {path}")
