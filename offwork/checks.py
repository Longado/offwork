from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from offwork.state import utc_now


CHECK_TIMEOUT_SECONDS = 2.0


def run_checks(commands: List[str], project: Path) -> Dict[str, Any]:
    if not commands:
        return {
            "schema_version": "offwork.checks/v1",
            "status": "not_run",
            "checks": [],
        }

    results: List[Dict[str, Any]] = []
    for command in commands:
        started_at = utc_now()
        try:
            argv = shlex.split(command)
            if not argv:
                raise ValueError("empty argv")
        except ValueError:
            results.append(
                {
                    "command": command,
                    "argv": [],
                    "cwd": str(project),
                    "status": "unavailable",
                    "returncode": None,
                    "started_at": started_at,
                    "finished_at": utc_now(),
                }
            )
            continue
        try:
            completed = subprocess.run(
                argv,
                cwd=str(project),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=CHECK_TIMEOUT_SECONDS,
                check=False,
                shell=False,
            )
            status = "passed" if completed.returncode == 0 else "failed"
            returncode = completed.returncode
        except (OSError, subprocess.TimeoutExpired):
            status = "unavailable"
            returncode = None
        results.append(
            {
                "command": command,
                "argv": argv,
                "cwd": str(project),
                "status": status,
                "returncode": returncode,
                "started_at": started_at,
                "finished_at": utc_now(),
            }
        )

    statuses = {item["status"] for item in results}
    if "unavailable" in statuses:
        aggregate = "unavailable"
    elif "failed" in statuses:
        aggregate = "failed"
    else:
        aggregate = "passed"
    return {
        "schema_version": "offwork.checks/v1",
        "status": aggregate,
        "checks": results,
    }
