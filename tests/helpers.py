from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict


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

    def init(self) -> Dict[str, Any]:
        result = self.run("init", "--project", str(self.project), "--json")
        if result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)
        return self.json_stdout(result)["data"]

    def add_task(
        self,
        title: str = "修复登录失败",
        goal: str = "恢复 Token 刷新行为",
    ) -> Dict[str, Any]:
        result = self.run(
            "task",
            "add",
            title,
            "--goal",
            goal,
            "--project",
            str(self.project),
            "--json",
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)
        return self.json_stdout(result)["data"]

    def write_context(self, value: Dict[str, Any], name: str = "context.json") -> Path:
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    @staticmethod
    def json_stdout(result: subprocess.CompletedProcess[str]) -> dict:
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise AssertionError("JSON stdout must be one object")
        return value
