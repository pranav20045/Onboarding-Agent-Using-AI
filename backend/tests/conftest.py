from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fixtures.build_fixture_repo import build_fixture_repo


@pytest.fixture(scope="session")
def fixture_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The `shopkit` fixture repo, built once per test session. Treat as read-only."""
    return build_fixture_repo(tmp_path_factory.mktemp("fixture") / "shopkit")


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Clear CodeAtlas-related env vars and point data_dir at a temp dir."""
    from app.config import Settings, get_settings

    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
