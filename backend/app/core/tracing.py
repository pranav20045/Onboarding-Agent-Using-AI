"""Optional Langfuse tracing. A no-op unless both Langfuse keys are set AND the optional
`tracing` dependency group is installed (`uv sync --extra tracing`).

One Langfuse "agent" observation per answer, with the question as input, the answer as output,
and usage + tool-call events as metadata. Secrets never enter trace payloads: only the
question, answer, tool names/arguments and usage numbers are sent.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from functools import lru_cache
from typing import Any

from app.config import Settings

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _client(public: str, secret: str, host: str) -> Any | None:
    try:
        from langfuse import Langfuse
    except ImportError:
        log.warning(
            "Langfuse keys set but the 'langfuse' package is not installed; tracing disabled"
        )
        return None
    return Langfuse(public_key=public, secret_key=secret, host=host)


class _Span:
    def __init__(self, obs: Any | None) -> None:
        self._obs = obs

    def finish(
        self, output: Any = None, metadata: dict | None = None, error: str | None = None
    ) -> None:
        if self._obs is None:
            return
        try:
            self._obs.update(output=output, metadata=metadata, level="ERROR" if error else None,
                             status_message=error)  # fmt: skip
        except Exception as exc:  # tracing must never break answering
            log.debug("Langfuse update failed: %s", exc)


@contextlib.contextmanager
def trace_answer(settings: Settings, name: str, question: str, metadata: dict) -> Iterator[_Span]:
    if not settings.langfuse_enabled:
        yield _Span(None)
        return
    client = _client(
        settings.langfuse_public_key.get_secret_value(),  # type: ignore[union-attr]
        settings.langfuse_secret_key.get_secret_value(),  # type: ignore[union-attr]
        settings.langfuse_host,
    )
    if client is None:
        yield _Span(None)
        return
    try:
        cm = client.start_as_current_observation(
            name=name, as_type="agent", input=question, metadata=metadata
        )
        obs = cm.__enter__()
    except Exception as exc:
        log.debug("Langfuse start failed: %s", exc)
        yield _Span(None)
        return
    try:
        yield _Span(obs)
    finally:
        with contextlib.suppress(Exception):
            cm.__exit__(None, None, None)
