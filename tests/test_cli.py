from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import TempProject


class StartupCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TempProject()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_help_runs_outside_repository_without_creating_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            result = self.temp.run("--help", cwd=cwd)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("trusted handoff receipts", result.stdout)
            self.assertFalse((cwd / ".offwork").exists())

    def test_version_runs_outside_repository_without_creating_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            result = self.temp.run("--version", cwd=cwd)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(result.stdout, r"offwork \d+\.\d+\.\d+")
            self.assertFalse((cwd / ".offwork").exists())

    def test_init_json_returns_one_envelope(self) -> None:
        result = self.temp.run(
            "init", "--project", str(self.temp.project), "--json"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        envelope = self.temp.json_stdout(result)
        self.assertEqual(envelope["schema_version"], "offwork.cli/v1")
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["command"], "init")
        self.assertEqual(envelope["data"]["project_path"], str(self.temp.project.resolve()))
        self.assertEqual(result.stderr, "")

    def test_argument_error_still_returns_one_json_envelope(self) -> None:
        result = self.temp.run("capture", "--json")

        self.assertNotEqual(result.returncode, 0)
        envelope = self.temp.json_stdout(result)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(result.stderr, "")

    def test_nested_argument_error_uses_stable_command_name(self) -> None:
        result = self.temp.run("task", "accept", "--json")

        self.assertNotEqual(result.returncode, 0)
        envelope = self.temp.json_stdout(result)
        self.assertEqual(envelope["command"], "task.accept")
        self.assertEqual(envelope["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(result.stderr, "")

    def test_json_help_is_one_envelope_in_either_parameter_order(self) -> None:
        for arguments in (("--json", "--help"), ("--help", "--json")):
            with self.subTest(arguments=arguments):
                result = self.temp.run(*arguments)

                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                envelope = self.temp.json_stdout(result)
                self.assertEqual(envelope["command"], "help")
                self.assertIn("trusted handoff receipts", envelope["data"]["text"])
                self.assertEqual(result.stderr, "")
                self.assertEqual(len(result.stdout.splitlines()), 1)

    def test_json_version_is_one_envelope_in_either_parameter_order(self) -> None:
        for arguments in (("--json", "--version"), ("--version", "--json")):
            with self.subTest(arguments=arguments):
                result = self.temp.run(*arguments)

                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                envelope = self.temp.json_stdout(result)
                self.assertEqual(envelope["command"], "version")
                self.assertRegex(envelope["data"]["text"], r"^offwork \d+\.\d+\.\d+$")
                self.assertEqual(result.stderr, "")
                self.assertEqual(len(result.stdout.splitlines()), 1)

    def test_json_without_a_command_returns_help_envelope(self) -> None:
        result = self.temp.run("--json")

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        envelope = self.temp.json_stdout(result)
        self.assertEqual(envelope["command"], "help")
        self.assertIn("usage: offwork", envelope["data"]["text"])
        self.assertEqual(result.stderr, "")

    def test_human_task_creation_escapes_untrusted_terminal_text(self) -> None:
        self.temp.init()
        title = "正常中文\n伪造状态\x1b[31m\u202e"

        result = self.temp.run(
            "task", "add", title, "--goal", "goal",
            "--project", str(self.temp.project)
        )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("正常中文", result.stdout)
        self.assertIn("\\n", result.stdout)
        self.assertIn("\\x1b", result.stdout)
        self.assertIn("\\u202e", result.stdout)
        self.assertNotIn("\x1b", result.stdout)
        self.assertNotIn("\u202e", result.stdout)


if __name__ == "__main__":
    unittest.main()
