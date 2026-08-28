from __future__ import annotations

import subprocess
from pathlib import Path

from offwork_capsule.project import capture_project_state


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)


def test_nested_project_capture_excludes_parent_and_offwork_metadata(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    project = repo / "nested" / "project"
    offwork = project / ".offwork"
    offwork.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "offwork@example.com")
    _git(repo, "config", "user.name", "Offwork Test")
    (repo / "parent.txt").write_text("before\n", encoding="utf-8")
    (project / "inside.txt").write_text("before\n", encoding="utf-8")
    (offwork / "tracked.json").write_text("before\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")

    (repo / "parent.txt").write_text("after\n", encoding="utf-8")
    (project / "inside.txt").write_text("after\n", encoding="utf-8")
    (project / "inside-new.txt").write_text("new\n", encoding="utf-8")
    (offwork / "tracked.json").write_text("after\n", encoding="utf-8")
    (offwork / "state.sqlite3").write_text("private\n", encoding="utf-8")

    state = capture_project_state(project)

    assert set(state["dirty_files"]) == {"inside.txt", "inside-new.txt"}
    serialized = "\n".join(
        [str(state["status_porcelain"]), str(state["diff_stat"])]
    )
    assert "parent.txt" not in serialized
    assert ".offwork" not in serialized
