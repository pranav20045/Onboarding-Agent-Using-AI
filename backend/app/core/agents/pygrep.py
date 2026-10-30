"""Python-regex grep, run as a *child process* so a catastrophic-backtracking pattern can be
killed by the parent's timeout (Python's `re` has no timeout of its own).

Protocol: argv = [root, pattern, glob-or-empty, max_results]; stdin = JSON list of
repo-relative files to search. Output lines: `path:line:text`.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

MAX_LINE = 300


def glob_to_regex(glob: str) -> re.Pattern[str]:
    """ripgrep-like globs: '*' within a segment, '**' across segments, '{a,b}' alternation.
    A glob without '/' matches the file name anywhere in the tree."""
    out = []
    i = 0
    while i < len(glob):
        c = glob[i]
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if glob.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "{":
            j = glob.find("}", i)
            if j == -1:
                out.append(re.escape(c))
            else:
                out.append("(?:" + "|".join(re.escape(p) for p in glob[i + 1 : j].split(",")) + ")")
                i = j + 1
                continue
        else:
            out.append(re.escape(c))
        i += 1
    body = "".join(out)
    if "/" not in glob:
        body = "(?:.*/)?" + body
    return re.compile(f"^{body}$")


def main() -> int:
    root, pattern, glob, max_results = Path(sys.argv[1]), sys.argv[2], sys.argv[3], int(sys.argv[4])
    files = json.loads(sys.stdin.read())
    rx = re.compile(pattern)
    gx = glob_to_regex(glob) if glob else None
    n = 0
    out = sys.stdout
    for rel in files:
        if gx and not gx.match(rel):
            continue
        try:
            text = (root / rel).read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.split("\n"), 1):
            if rx.search(line):
                out.write(f"{rel}:{lineno}:{line.rstrip(chr(13))[:MAX_LINE]}\n")
                n += 1
                if n >= max_results:
                    return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
