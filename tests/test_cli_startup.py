from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _run(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["XDG_DATA_HOME"] = str(tmp_path / "xdg-data")
    return subprocess.run(
        [str(ROOT / "bin" / "offwork"), *arguments],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_help_and_version_are_database_free(tmp_path: Path) -> None:
    help_result = _run(tmp_path, "--help")
    version_result = _run(tmp_path, "--version")

    assert help_result.returncode == 0
    assert "usage: offwork" in help_result.stdout
    assert version_result.returncode == 0
    assert version_result.stdout.strip() == "offwork 0.2.0"
    assert version_result.stderr == ""
    assert not (tmp_path / ".offwork").exists()
    assert not (tmp_path / "xdg-data" / "offwork" / "registry.sqlite3").exists()
