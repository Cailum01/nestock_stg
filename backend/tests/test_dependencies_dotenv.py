from pathlib import Path

import pytest

from app.core import dependencies


def test_load_local_env_reads_dotenv_without_overriding_real_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("NESTOCK_COSMOS_DATABASE=from-file\nNESTOCK_X=from-file\n")
    monkeypatch.setattr(dependencies, "BACKEND_DIR", tmp_path)
    monkeypatch.delenv(dependencies.SKIP_DOTENV_VAR, raising=False)
    monkeypatch.setenv("NESTOCK_COSMOS_DATABASE", "from-shell")
    monkeypatch.delenv("NESTOCK_X", raising=False)

    dependencies._load_local_env()

    assert dependencies.os.environ["NESTOCK_X"] == "from-file"
    assert dependencies.os.environ["NESTOCK_COSMOS_DATABASE"] == "from-shell"
    monkeypatch.delenv("NESTOCK_X", raising=False)


def test_load_local_env_is_skipped_when_flag_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("NESTOCK_Y=from-file\n")
    monkeypatch.setattr(dependencies, "BACKEND_DIR", tmp_path)
    monkeypatch.setenv(dependencies.SKIP_DOTENV_VAR, "1")
    monkeypatch.delenv("NESTOCK_Y", raising=False)

    dependencies._load_local_env()

    assert "NESTOCK_Y" not in dependencies.os.environ
