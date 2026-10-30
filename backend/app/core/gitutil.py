"""Safe `git` invocation: argument lists only (never a shell string), no prompts, no hooks."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_TERMINAL_PROMPT="0",  # never ask for credentials (private repos fail fast)
        GIT_ASKPASS="echo",
        GIT_CONFIG_NOSYSTEM="1",
        LC_ALL="C",
    )
    return env


# Applied to every invocation: no hooks from anywhere, no fsmonitor, symlinks as plain files.
_SAFE_CONFIG = [
    "-c", f"core.hooksPath={os.devnull}",
    "-c", "core.fsmonitor=false",
    "-c", "core.symlinks=false",
    "-c", "core.longpaths=true",
]  # fmt: skip


def run_git(
    args: list[str],
    cwd: Path | None = None,
    timeout: float = 120,
    check: bool = True,
    extra_config: list[str] | None = None,
) -> str:
    try:
        result = subprocess.run(
            ["git", *_SAFE_CONFIG, *(extra_config or []), *args],
            cwd=cwd,
            env=git_env(),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} timed out after {timeout}s") from exc
    stdout = result.stdout.decode("utf-8", errors="replace")
    if check and result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(f"git {args[0]} failed ({result.returncode}): {stderr[-500:]}")
    return stdout


# For clones of public GitHub URLs: only https is allowed (no file://, ssh, ext:: transports).
HTTPS_ONLY = ["-c", "protocol.allow=never", "-c", "protocol.https.allow=always"]
