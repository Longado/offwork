from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "bin" / "offwork"


class TempProject:
    def __init__(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.project = self.root / "project"
        self.project.mkdir()

    def cleanup(self) -> None:
        self._temporary_directory.cleanup()

    def run(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(CLI), *args],
            cwd=str(cwd or self.root),
            text=True,
            capture_output=True,
            check=False,
        )

    @staticmethod
    def json_stdout(result: subprocess.CompletedProcess[str]) -> dict:
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise AssertionError("JSON stdout must be one object")
        return value

