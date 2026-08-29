from __future__ import annotations

import json
import os
import shlex
import sqlite3
import sys
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path
from unittest import mock

from offwork import checks
from offwork.errors import OffworkError
from offwork.state import StateService
from tests.helpers import TempProject


CONTEXT = {
    "summary": "Prepared a bounded check-runner handoff",
    "agent_claims": [],
    "unknowns": [],
    "open_loops": [],
    "next_step": "Review the captured check result",
}


def python_command(source: str, *arguments: str) -> str:
    return shlex.join([sys.executable, "-c", source, *arguments])


class CheckRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.project = Path(self.directory.name) / "project"
        self.project.mkdir()

    def tearDown(self) -> None:
        self.directory.cleanup()

    @unittest.skipUnless(os.name == "posix", "process-group checks require POSIX")
    def test_timeout_terminates_descendant_holding_capture_pipes(self) -> None:
        marker = self.project / "late-mutation"
        child = (
            "import pathlib,time; "
            "time.sleep(0.30); "
            f"pathlib.Path({str(marker)!r}).write_text('mutated'); "
            "time.sleep(0.30)"
        )
        parent = (
            "import subprocess,sys,time; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
            "time.sleep(5)"
        )

        started = time.monotonic()
        with mock.patch.object(checks, "CHECK_TIMEOUT_SECONDS", 0.10), mock.patch.object(
            checks, "CHECK_TERMINATION_GRACE_SECONDS", 0.05, create=True
        ), mock.patch.object(
            checks, "CHECK_KILL_GRACE_SECONDS", 0.05, create=True
        ):
            result = checks.run_checks([python_command(parent)], self.project)
        elapsed = time.monotonic() - started
        time.sleep(0.35)

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["checks"][0]["status"], "unavailable")
        self.assertLess(elapsed, 0.45)
        self.assertFalse(marker.exists())

    @unittest.skipUnless(os.name == "posix", "process-group checks require POSIX")
    def test_total_deadline_marks_remaining_checks_unavailable_without_starting_them(
        self,
    ) -> None:
        marker = self.project / "must-not-run"
        commands = [
            "/usr/bin/true",
            "/bin/sleep 5",
            python_command(
                "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ran')",
                str(marker),
            ),
        ]

        started = time.monotonic()
        with mock.patch.object(checks, "CHECK_TIMEOUT_SECONDS", 0.75), mock.patch.object(
            checks, "TOTAL_CHECK_TIMEOUT_SECONDS", 0.50, create=True
        ), mock.patch.object(
            checks, "CHECK_TERMINATION_GRACE_SECONDS", 0.03, create=True
        ), mock.patch.object(
            checks, "CHECK_KILL_GRACE_SECONDS", 0.03, create=True
        ):
            result = checks.run_checks(commands, self.project)
        elapsed = time.monotonic() - started

        self.assertEqual(
            [item["status"] for item in result["checks"]],
            ["passed", "unavailable", "unavailable"],
        )
        self.assertEqual(result["status"], "unavailable")
        self.assertLess(elapsed, 1.50)
        self.assertFalse(marker.exists())

    def test_large_stdout_and_stderr_are_drained_with_bounded_memory(self) -> None:
        source = (
            "import os; "
            "chunk=b'x'*65536; "
            "[(os.write(1,chunk),os.write(2,chunk)) for _ in range(128)]"
        )

        tracemalloc.start()
        try:
            result = checks.run_checks([python_command(source)], self.project)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

        self.assertEqual(result["status"], "passed")
        self.assertLess(peak, 2 * 1024 * 1024)
        self.assertNotIn("stdout", result["checks"][0])
        self.assertNotIn("stderr", result["checks"][0])

    def test_rejects_secret_bearing_argv_before_execution(self) -> None:
        marker = self.project / "executed"
        source = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ran')"
        cases = [
            ["Authorization", "Bearer secret"],
            ["aUtHoRiZaTiOn: Bearer secret"],
            ["-HAuthorization: Bearer secret"],
            ["--PaSsWoRd", "secret"],
            ["--TOKEN=secret"],
            ["--Api-Key=secret"],
            ["https://alice:secret@example.test/check"],
            ["--url=https://alice:secret@example.test/check"],
        ]

        for suffix in cases:
            with self.subTest(argv=suffix):
                marker.unlink(missing_ok=True)
                command = shlex.join(
                    [sys.executable, "-c", source, str(marker), *suffix]
                )
                with self.assertRaises(OffworkError) as raised:
                    checks.run_checks([command], self.project)
                self.assertEqual(raised.exception.code, "UNSAFE_CHECK_ARGUMENT")
                self.assertFalse(marker.exists())

    def test_secret_in_later_check_is_rejected_before_any_check_starts(self) -> None:
        marker = self.project / "executed"
        first = python_command(
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ran')",
            str(marker),
        )
        second = python_command("raise SystemExit(0)", "--password=secret")

        with self.assertRaises(OffworkError) as raised:
            checks.run_checks([first, second], self.project)

        self.assertEqual(raised.exception.code, "UNSAFE_CHECK_ARGUMENT")
        self.assertFalse(marker.exists())

    def test_rejects_secrets_nested_inside_an_argv_string_before_execution(self) -> None:
        marker = self.project / "executed"
        cases = [
            "url='https://alice:nested-secret@example.test/check'",
            "args=['--token=nested-secret']",
            "headers={'Authorization': 'Bearer nested-secret'}",
        ]

        for secret_source in cases:
            with self.subTest(source=secret_source):
                marker.unlink(missing_ok=True)
                source = (
                    f"{secret_source}; "
                    "import pathlib; pathlib.Path('executed').write_text('ran')"
                )
                with self.assertRaises(OffworkError) as raised:
                    checks.run_checks([python_command(source)], self.project)
                self.assertEqual(raised.exception.code, "UNSAFE_CHECK_ARGUMENT")
                self.assertFalse(marker.exists())

    def test_malformed_secret_command_is_rejected_before_any_check_starts(self) -> None:
        marker = self.project / "executed"
        first = python_command(
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ran')",
            str(marker),
        )
        malformed_cases = [
            "/usr/bin/true --TOKEN='secret",
            "/usr/bin/true -H 'Authorization: Bearer path/secret",
            "/usr/bin/true 'https://alice:secret@example.test/check",
        ]

        for malformed in malformed_cases:
            with self.subTest(command=malformed):
                marker.unlink(missing_ok=True)
                with self.assertRaises(OffworkError) as raised:
                    checks.run_checks([first, malformed], self.project)
                self.assertEqual(raised.exception.code, "UNSAFE_CHECK_ARGUMENT")
                self.assertFalse(marker.exists())

    def test_unparsable_command_is_rejected_before_any_check_starts(self) -> None:
        marker = self.project / "executed"
        first = python_command(
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ran')",
            str(marker),
        )
        malformed = "/usr/bin/true -H Authoriza\\tion:\\ Bearer\\ path/secret '"

        with self.assertRaises(OffworkError) as raised:
            checks.run_checks([first, malformed], self.project)

        self.assertEqual(raised.exception.code, "INVALID_CHECK_COMMAND")
        self.assertEqual(raised.exception.details, {"command_index": 1})
        self.assertFalse(marker.exists())

    def test_authorization_header_with_slash_is_rejected_before_execution(self) -> None:
        marker = self.project / "executed"
        source = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ran')"
        cases = [
            ["Authorization: Bearer path/secret"],
            ["-HAuthorization: Bearer path/secret"],
            ["--header=Authorization: Bearer path/secret"],
        ]

        for suffix in cases:
            with self.subTest(argv=suffix):
                marker.unlink(missing_ok=True)
                command = shlex.join(
                    [sys.executable, "-c", source, str(marker), *suffix]
                )
                with self.assertRaises(OffworkError) as raised:
                    checks.run_checks([command], self.project)
                self.assertEqual(raised.exception.code, "UNSAFE_CHECK_ARGUMENT")
                self.assertFalse(marker.exists())

    def test_authorization_named_path_is_not_treated_as_a_header(self) -> None:
        argument = "/tmp/Authorization:notes/path"
        command = python_command(
            "import pathlib,sys; pathlib.Path('argument.txt').write_text(sys.argv[1])",
            argument,
        )

        result = checks.run_checks([command], self.project)

        self.assertEqual(result["status"], "passed")
        self.assertEqual((self.project / "argument.txt").read_text(), argument)

    def _assert_exception_terminates_descendant(
        self, interruption: BaseException, label: str
    ) -> None:
        ready = self.project / f"{label}-ready"
        marker = self.project / f"{label}-late-mutation"
        child = (
            "import pathlib,time; "
            "time.sleep(0.10); "
            f"pathlib.Path({str(marker)!r}).write_text('mutated'); "
            "time.sleep(0.20)"
        )
        parent = (
            "import pathlib,subprocess,sys,time; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
            f"pathlib.Path({str(ready)!r}).write_text('ready'); "
            "time.sleep(5)"
        )
        original_drain = checks._drain_ready_streams
        raised = False

        def interrupt_once(selector: object, buffers: object, wait: float) -> None:
            nonlocal raised
            original_drain(selector, buffers, min(wait, 0.01))
            if ready.exists() and not raised:
                raised = True
                raise interruption

        with mock.patch.object(
            checks, "_drain_ready_streams", side_effect=interrupt_once
        ), self.assertRaises(type(interruption)):
            checks.run_checks([python_command(parent)], self.project)
        time.sleep(0.60)

        self.assertTrue(raised)
        self.assertFalse(marker.exists())

    @unittest.skipUnless(os.name == "posix", "process-group checks require POSIX")
    def test_runtime_exception_terminates_descendant_process_group(self) -> None:
        self._assert_exception_terminates_descendant(RuntimeError("interrupted"), "error")

    @unittest.skipUnless(os.name == "posix", "process-group checks require POSIX")
    def test_keyboard_interrupt_terminates_descendant_process_group(self) -> None:
        self._assert_exception_terminates_descendant(KeyboardInterrupt(), "keyboard")

    def test_local_wrapper_path_remains_allowed(self) -> None:
        wrapper = self.project / "bin" / "token-wrapper"
        wrapper.parent.mkdir()
        wrapper.write_text("marker.txt", encoding="utf-8")
        command = python_command(
            "import pathlib,sys; "
            "pathlib.Path(sys.argv[1]).with_name(pathlib.Path(sys.argv[1]).read_text()).write_text('ok')",
            str(wrapper),
        )

        result = checks.run_checks([command], self.project)

        self.assertEqual(result["status"], "passed")
        self.assertTrue((wrapper.parent / "marker.txt").is_file())

    def test_check_uses_argv_shell_false_and_canonical_cwd(self) -> None:
        project_link = Path(self.directory.name) / "project-link"
        project_link.symlink_to(self.project, target_is_directory=True)
        injected = self.project / "injected"
        argument = f"literal; touch {injected}"
        command = python_command(
            "import os,pathlib,sys; "
            "pathlib.Path('cwd.txt').write_text(os.getcwd()); "
            "pathlib.Path('argv.txt').write_text(sys.argv[1])",
            argument,
        )

        result = checks.run_checks([command], project_link)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["checks"][0]["cwd"], str(self.project.resolve()))
        self.assertEqual((self.project / "cwd.txt").read_text(), str(self.project.resolve()))
        self.assertEqual((self.project / "argv.txt").read_text(), argument)
        self.assertFalse(injected.exists())

    def test_aggregation_states_remain_explicit(self) -> None:
        passed = python_command("raise SystemExit(0)")
        failed = python_command("raise SystemExit(3)")
        unavailable = "/definitely/missing/offwork-check"

        self.assertEqual(checks.run_checks([], self.project)["status"], "not_run")
        self.assertEqual(checks.run_checks([passed], self.project)["status"], "passed")
        self.assertEqual(
            checks.run_checks([passed, failed], self.project)["status"], "failed"
        )
        self.assertEqual(
            checks.run_checks([passed, unavailable], self.project)["status"],
            "unavailable",
        )


class CheckCredentialCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TempProject()
        self.temp.init()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_secret_argument_returns_stable_error_without_publishing_capsule(self) -> None:
        marker = self.temp.project / "executed"
        command = python_command(
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ran')",
            str(marker),
            "--token=secret",
        )
        task = StateService(self.temp.project / ".offwork").add_task(
            "legacy unsafe task",
            "capture must still reject stored unsafe checks",
            [command],
        )
        context = self.temp.write_context(CONTEXT)

        result = self.temp.run(
            "capture",
            "--task",
            task["task_id"],
            "--context",
            str(context),
            "--project",
            str(self.temp.project),
            "--json",
        )

        self.assertNotEqual(result.returncode, 0)
        envelope = json.loads(result.stdout)
        self.assertEqual(envelope["error"]["code"], "UNSAFE_CHECK_ARGUMENT")
        self.assertFalse(marker.exists())
        capsules = self.temp.project / ".offwork" / "capsules"
        self.assertEqual(list(capsules.iterdir()), [])

    def test_task_add_rejects_invalid_checks_without_persisting_them(self) -> None:
        cases = [
            (
                "/usr/bin/true --token=path/secret",
                "UNSAFE_CHECK_ARGUMENT",
            ),
            (
                "/usr/bin/true -H Authoriza\\tion:\\ Bearer\\ path/secret '",
                "INVALID_CHECK_COMMAND",
            ),
            (
                python_command(
                    "url='https://alice:nested-secret@example.test/check'"
                ),
                "UNSAFE_CHECK_ARGUMENT",
            ),
            (
                python_command("args=['--token=nested-secret']"),
                "UNSAFE_CHECK_ARGUMENT",
            ),
            (
                python_command(
                    "headers={'Authorization': 'Bearer nested-secret'}"
                ),
                "UNSAFE_CHECK_ARGUMENT",
            ),
        ]

        for command, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                result = self.temp.run(
                    "task",
                    "add",
                    "credential regression",
                    "--goal",
                    "reject before persistence",
                    "--check",
                    command,
                    "--project",
                    str(self.temp.project),
                    "--json",
                )

                self.assertNotEqual(result.returncode, 0)
                envelope = json.loads(result.stdout)
                self.assertEqual(envelope["error"]["code"], expected_code)
                self.assertNotIn(command, result.stdout)
                self.assertNotIn("path/secret", result.stdout)
                self.assertNotIn("nested-secret", result.stdout)

        database = self.temp.project / ".offwork" / "state.sqlite3"
        connection = sqlite3.connect(database)
        try:
            task_count = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(task_count, 0)
        self.assertNotIn(b"path/secret", database.read_bytes())
        self.assertNotIn(b"nested-secret", database.read_bytes())


if __name__ == "__main__":
    unittest.main()
