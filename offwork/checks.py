from __future__ import annotations

import os
import re
import selectors
import shlex
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

from offwork.errors import OffworkError
from offwork.state import utc_now


CHECK_TIMEOUT_SECONDS = 2.0
TOTAL_CHECK_TIMEOUT_SECONDS = 5.0
CHECK_TERMINATION_GRACE_SECONDS = 0.20
CHECK_KILL_GRACE_SECONDS = 0.20
MAX_CAPTURED_OUTPUT_BYTES = 64 * 1024
OUTPUT_READ_BYTES = 64 * 1024

_SECRET_OPTIONS = ("--password", "--token", "--api-key")
_AUTHORIZATION_HEADER = re.compile(
    r"^authorization(?:\s*[:=]|\s*$)",
    re.IGNORECASE,
)
_URL_USER_INFO = re.compile(
    r"[a-z][a-z0-9+.-]*://[^/@\s]+@",
    re.IGNORECASE,
)
_RAW_SECRET_OPTION = re.compile(
    r"(?:^|[\s\"'])--(?:password|token|api-key)(?:=|[\s\"']|$)",
    re.IGNORECASE,
)
_RAW_AUTHORIZATION_HEADER = re.compile(
    r"(?:^|[\s\"'])(?:-h\s*=?\s*|--header(?:=|\s+))?[\"']?"
    r"authorization(?:\s*[:=]|\s*(?:[\"']|$))",
    re.IGNORECASE,
)
_RAW_URL_USER_INFO = re.compile(
    r"[a-z][a-z0-9+.-]*://[^/\s\"']+@",
    re.IGNORECASE,
)


def _raise_unsafe_check_argument(**details: int) -> None:
    raise OffworkError(
        "UNSAFE_CHECK_ARGUMENT",
        "Check arguments must not contain credentials",
        details=details,
    )


def _is_authorization_header(argument: str) -> bool:
    candidate = argument.strip()
    folded = candidate.casefold()
    if folded.startswith("--header="):
        candidate = candidate[len("--header=") :].lstrip()
    elif folded.startswith("-h") and not folded.startswith("--"):
        candidate = candidate[2:].lstrip("= \t")
    return _AUTHORIZATION_HEADER.match(candidate) is not None


def _validate_raw_command(command: str, command_index: int) -> None:
    if (
        _RAW_SECRET_OPTION.search(command) is not None
        or _RAW_AUTHORIZATION_HEADER.search(command) is not None
        or _RAW_URL_USER_INFO.search(command) is not None
    ):
        _raise_unsafe_check_argument(command_index=command_index)


def _validate_argv(argv: List[str]) -> None:
    for index, argument in enumerate(argv):
        folded = argument.casefold()
        if any(
            folded == option or folded.startswith(f"{option}=")
            for option in _SECRET_OPTIONS
        ):
            _raise_unsafe_check_argument(argument_index=index)
        if _is_authorization_header(argument) or _URL_USER_INFO.search(argument):
            _raise_unsafe_check_argument(argument_index=index)


def validate_check_commands(commands: List[str]) -> List[List[str]]:
    parsed_commands: List[List[str]] = []
    for command_index, command in enumerate(commands):
        _validate_raw_command(command, command_index)
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise OffworkError(
                "INVALID_CHECK_COMMAND",
                "Check command could not be parsed",
                details={"command_index": command_index},
            ) from exc
        if not argv:
            raise OffworkError(
                "INVALID_CHECK_COMMAND",
                "Check command could not be parsed",
                details={"command_index": command_index},
            )
        _validate_argv(argv)
        parsed_commands.append(argv)
    return parsed_commands


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_process_group(process_group: int, action: signal.Signals) -> None:
    try:
        os.killpg(process_group, action)
    except ProcessLookupError:
        pass


def _close_stream(selector: selectors.BaseSelector, stream: Any) -> None:
    try:
        selector.unregister(stream)
    except (KeyError, ValueError):
        pass
    try:
        stream.close()
    except OSError:
        pass


def _drain_ready_streams(
    selector: selectors.BaseSelector,
    buffers: Dict[int, bytearray],
    wait_seconds: float,
) -> None:
    for key, _ in selector.select(max(0.0, wait_seconds)):
        stream = key.fileobj
        try:
            chunk = os.read(key.fd, OUTPUT_READ_BYTES)
        except BlockingIOError:
            continue
        except OSError:
            _close_stream(selector, stream)
            continue
        if not chunk:
            _close_stream(selector, stream)
            continue
        buffer = buffers[key.fd]
        remaining = MAX_CAPTURED_OUTPUT_BYTES - len(buffer)
        if remaining > 0:
            buffer.extend(chunk[:remaining])


def _execution_complete(
    process: subprocess.Popen[bytes], selector: selectors.BaseSelector
) -> bool:
    return (
        process.poll() is not None
        and not selector.get_map()
        and not _process_group_exists(process.pid)
    )


def _wait_until(
    process: subprocess.Popen[bytes],
    selector: selectors.BaseSelector,
    buffers: Dict[int, bytearray],
    deadline: float,
) -> bool:
    while True:
        if _execution_complete(process, selector):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        _drain_ready_streams(selector, buffers, min(remaining, 0.05))


def _terminate_group(
    process: subprocess.Popen[bytes],
    selector: selectors.BaseSelector,
    buffers: Dict[int, bytearray],
) -> None:
    _signal_process_group(process.pid, signal.SIGTERM)
    if _wait_until(
        process,
        selector,
        buffers,
        time.monotonic() + CHECK_TERMINATION_GRACE_SECONDS,
    ):
        return
    _signal_process_group(process.pid, signal.SIGKILL)
    _wait_until(
        process,
        selector,
        buffers,
        time.monotonic() + CHECK_KILL_GRACE_SECONDS,
    )


def _run_check(
    argv: List[str], canonical_project: Path, deadline: float
) -> tuple[str, int | None]:
    selector = selectors.DefaultSelector()
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(canonical_project),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
    except OSError:
        selector.close()
        return "unavailable", None
    except BaseException:
        selector.close()
        raise

    buffers: Dict[int, bytearray] = {}
    streams = (process.stdout, process.stderr)
    try:
        for stream in streams:
            if stream is None:
                continue
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
            buffers[stream.fileno()] = bytearray()

        if not _wait_until(process, selector, buffers, deadline):
            _terminate_group(process, selector, buffers)
            return "unavailable", None
        return (
            ("passed" if process.returncode == 0 else "failed"),
            process.returncode,
        )
    except BaseException:
        _terminate_group(process, selector, buffers)
        raise
    finally:
        for stream in streams:
            if stream is not None:
                _close_stream(selector, stream)
        selector.close()
        if process.poll() is None or _process_group_exists(process.pid):
            _signal_process_group(process.pid, signal.SIGKILL)
            try:
                process.wait(timeout=CHECK_KILL_GRACE_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                pass


def run_checks(commands: List[str], project: Path) -> Dict[str, Any]:
    if not commands:
        return {
            "schema_version": "offwork.checks/v1",
            "status": "not_run",
            "checks": [],
        }

    parsed_commands = validate_check_commands(commands)

    canonical_project = project.resolve(strict=True)
    total_deadline = time.monotonic() + TOTAL_CHECK_TIMEOUT_SECONDS
    results: List[Dict[str, Any]] = []
    for command, argv in zip(commands, parsed_commands):
        started_at = utc_now()
        remaining = total_deadline - time.monotonic()
        if remaining <= 0:
            status = "unavailable"
            returncode = None
        else:
            deadline = min(
                total_deadline,
                time.monotonic() + CHECK_TIMEOUT_SECONDS,
            )
            status, returncode = _run_check(argv, canonical_project, deadline)
        results.append(
            {
                "command": command,
                "argv": argv,
                "cwd": str(canonical_project),
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
