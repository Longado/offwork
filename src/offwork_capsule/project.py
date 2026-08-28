from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple


def _git(project: Path, *args: str) -> Optional[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _git_output(project: Path, *args: str) -> Optional[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _relative_project_path(
    git_root: Path, project: Path, git_path: str
) -> Optional[str]:
    lexical = PurePosixPath(git_path)
    if lexical.is_absolute() or ".." in lexical.parts:
        return None
    parts = [part for part in lexical.parts if part not in ("", ".")]
    if not parts:
        return None
    candidate = git_root.joinpath(*parts)
    try:
        return candidate.relative_to(project).as_posix()
    except ValueError:
        return None


def _is_offwork_metadata(path: Optional[str]) -> bool:
    return path == ".offwork" or (
        isinstance(path, str) and path.startswith(".offwork/")
    )


def _bounded_status(
    raw_status: str, git_root: Path, project: Path
) -> Tuple[str, List[str]]:
    records = raw_status.split("\0")
    lines: List[str] = []
    dirty_files: List[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record or len(record) < 4:
            continue
        code = record[:2]
        current_git_path = record[3:]
        current = _relative_project_path(git_root, project, current_git_path)
        if _is_offwork_metadata(current):
            current = None

        renamed = "R" in code or "C" in code
        original: Optional[str] = None
        if renamed and index < len(records):
            original_git_path = records[index]
            index += 1
            original = _relative_project_path(git_root, project, original_git_path)
            if _is_offwork_metadata(original):
                original = None

        dirty_path = current or original
        if dirty_path is None:
            continue
        dirty_files.append(dirty_path)
        if renamed and current is not None and original is not None:
            lines.append("%s %s -> %s" % (code, original, current))
        else:
            lines.append("%s %s" % (code, dirty_path))

    return "\n".join(lines), dirty_files


def capture_project_state(project_root: Path) -> Dict[str, object]:
    project = Path(project_root).resolve()
    inside = _git(project, "rev-parse", "--is-inside-work-tree") == "true"
    state: Dict[str, object] = {
        "project_path": str(project),
        "is_git_repo": inside,
        "branch": None,
        "head": None,
        "dirty_files": [],
        "status_porcelain": "",
        "diff_stat": "",
    }
    if not inside:
        return state

    git_root_text = _git(project, "rev-parse", "--show-toplevel")
    if not git_root_text:
        return state
    git_root = Path(git_root_text).resolve()
    raw_status = _git_output(
        project,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        ".",
        ":(exclude).offwork",
        ":(exclude).offwork/**",
    ) or ""
    status, dirty_files = _bounded_status(raw_status, git_root, project)
    state.update(
        {
            "branch": _git(project, "branch", "--show-current"),
            "head": _git(project, "rev-parse", "--short", "HEAD"),
            "dirty_files": dirty_files,
            "status_porcelain": status,
            "diff_stat": _git(
                project,
                "diff",
                "--stat",
                "HEAD",
                "--",
                ".",
                ":(exclude).offwork",
                ":(exclude).offwork/**",
            )
            or "",
        }
    )
    return state
