from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

from offwork.capsule import _read_private_member
from offwork.cli import main
from offwork.errors import OffworkError
from tests.helpers import TempProject


CONTEXT = {
    "summary": "已实现 Token 刷新修复并补充测试",
    "agent_claims": ["登录失败已经修复", "测试全部通过"],
    "unknowns": ["旧 Token 迁移行为尚未确认"],
    "open_loops": [
        {
            "title": "确认旧 Token 的迁移行为",
            "disposition": "resolve",
            "note": "先运行迁移测试",
        }
    ],
    "next_step": "运行旧 Token 迁移测试",
}


class CapsuleCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TempProject()
        self.temp.init()
        self.task = self.temp.add_task()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def capture(self, context: dict = CONTEXT, name: str = "context.json") -> dict:
        context_path = self.temp.write_context(context, name)
        result = self.temp.run(
            "capture",
            "--task",
            self.task["task_id"],
            "--context",
            str(context_path),
            "--project",
            str(self.temp.project),
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return self.temp.json_stdout(result)["data"]

    def test_capture_preserves_handoff_context_without_promoting_claims(self) -> None:
        receipt = self.capture()

        self.assertEqual(receipt["agent_claimed"]["items"], CONTEXT["agent_claims"])
        self.assertEqual(receipt["unknowns"], CONTEXT["unknowns"])
        self.assertEqual(receipt["open_loops"], CONTEXT["open_loops"])
        self.assertEqual(receipt["next_step"], CONTEXT["next_step"])
        self.assertEqual(receipt["auto_checked"]["status"], "not_run")
        self.assertEqual(receipt["auto_checked"]["checks"], [])
        self.assertEqual(receipt["human_acceptance"]["status"], "pending")
        self.assertEqual(receipt["workspace_freshness"]["status"], "unavailable")

    def test_capture_returns_explicit_capsule_and_revisions(self) -> None:
        receipt = self.capture()

        self.assertRegex(receipt["capsule"]["capsule_id"], r"^capsule-[0-9a-f]{32}$")
        self.assertEqual(receipt["task"]["captured_revision"], 2)
        self.assertEqual(receipt["task"]["current_revision"], 2)
        capsule_dir = (
            self.temp.project
            / ".offwork"
            / "capsules"
            / receipt["capsule"]["capsule_id"]
        )
        self.assertTrue((capsule_dir / "capsule.json").is_file())
        self.assertTrue((capsule_dir / "checks.json").is_file())
        self.assertTrue((capsule_dir / "restore-test.json").is_file())
        self.assertTrue((capsule_dir / "manifest.json").is_file())

    def test_capture_creates_exact_private_capsule_under_restrictive_umask(self) -> None:
        context_path = self.temp.write_context(CONTEXT)

        result = self.temp.run(
            "capture",
            "--task",
            self.task["task_id"],
            "--context",
            str(context_path),
            "--project",
            str(self.temp.project),
            "--json",
            umask=0o777,
        )

        capsules = self.temp.project / ".offwork" / "capsules"
        staging_directories = list(capsules.glob(".staging-*"))
        for staging in staging_directories:
            staging.rmdir()
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        receipt = self.temp.json_stdout(result)["data"]
        capsule = capsules / receipt["capsule"]["capsule_id"]
        self.assertEqual(os.stat(capsule).st_mode & 0o777, 0o700)
        for member in capsule.iterdir():
            self.assertEqual(os.stat(member).st_mode & 0o777, 0o600)
        self.assertEqual(staging_directories, [])

    def test_staging_setup_failure_returns_one_json_error_and_no_residue(self) -> None:
        context_path = self.temp.write_context(CONTEXT)
        stdout = io.StringIO()
        arguments = [
            "capture",
            "--task",
            self.task["task_id"],
            "--context",
            str(context_path),
            "--project",
            str(self.temp.project),
            "--json",
        ]

        setup_failure = PermissionError("staging denied")
        with mock.patch(
            "offwork.capsule.Path.mkdir", side_effect=setup_failure
        ), mock.patch(
            "offwork.state.os.mkdir", side_effect=setup_failure
        ), contextlib.redirect_stdout(stdout):
            try:
                returncode = main(arguments)
            except OSError as exc:
                self.fail(f"capture leaked an OS error instead of a JSON envelope: {exc}")

        self.assertNotEqual(returncode, 0)
        envelope = json.loads(stdout.getvalue())
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "UNSAFE_STATE_PATH")
        capsules = self.temp.project / ".offwork" / "capsules"
        self.assertEqual(list(capsules.glob(".staging-*")), [])

    def test_payload_write_failure_returns_one_json_error_and_no_residue(self) -> None:
        context_path = self.temp.write_context(CONTEXT)
        stdout = io.StringIO()
        arguments = [
            "capture",
            "--task",
            self.task["task_id"],
            "--context",
            str(context_path),
            "--project",
            str(self.temp.project),
            "--json",
        ]

        with mock.patch(
            "offwork.capsule._write_private",
            side_effect=PermissionError("payload write denied"),
        ), contextlib.redirect_stdout(stdout):
            try:
                returncode = main(arguments)
            except OSError as exc:
                self.fail(f"capture leaked an OS error instead of a JSON envelope: {exc}")

        self.assertNotEqual(returncode, 0)
        envelope = json.loads(stdout.getvalue())
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "CAPSULE_PUBLICATION_FAILED")
        capsules = self.temp.project / ".offwork" / "capsules"
        self.assertEqual(list(capsules.glob(".staging-*")), [])

    def test_publication_preserves_existing_offwork_error(self) -> None:
        context_path = self.temp.write_context(CONTEXT)
        stdout = io.StringIO()
        arguments = [
            "capture",
            "--task",
            self.task["task_id"],
            "--context",
            str(context_path),
            "--project",
            str(self.temp.project),
            "--json",
        ]
        expected = OffworkError("PUBLICATION_SENTINEL", "preserve this error")

        with mock.patch(
            "offwork.capsule._write_private",
            side_effect=expected,
        ), contextlib.redirect_stdout(stdout):
            returncode = main(arguments)

        self.assertNotEqual(returncode, 0)
        envelope = json.loads(stdout.getvalue())
        self.assertEqual(envelope["error"]["code"], "PUBLICATION_SENTINEL")
        capsules = self.temp.project / ".offwork" / "capsules"
        self.assertEqual(list(capsules.glob(".staging-*")), [])

    def test_invalid_context_returns_stable_json_error(self) -> None:
        invalid = dict(CONTEXT)
        del invalid["next_step"]
        path = self.temp.write_context(invalid, "invalid.json")

        result = self.temp.run(
            "capture",
            "--task",
            self.task["task_id"],
            "--context",
            str(path),
            "--project",
            str(self.temp.project),
            "--json",
        )

        self.assertNotEqual(result.returncode, 0)
        envelope = self.temp.json_stdout(result)
        self.assertEqual(envelope["error"]["code"], "INVALID_CAPTURE_CONTEXT")

    def test_unknown_task_does_not_publish_capsule(self) -> None:
        path = self.temp.write_context(CONTEXT)
        result = self.temp.run(
            "capture",
            "--task",
            "task-missing",
            "--context",
            str(path),
            "--project",
            str(self.temp.project),
            "--json",
        )

        self.assertNotEqual(result.returncode, 0)
        envelope = self.temp.json_stdout(result)
        self.assertEqual(envelope["error"]["code"], "TASK_NOT_FOUND")
        capsules = self.temp.project / ".offwork" / "capsules"
        self.assertEqual(list(capsules.iterdir()), [])


class TaskCreationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TempProject()
        self.temp.init()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_task_add_creates_revision_one(self) -> None:
        task = self.temp.add_task()

        self.assertRegex(task["task_id"], r"^task-[0-9a-f]{32}$")
        self.assertEqual(task["title"], "修复登录失败")
        self.assertEqual(task["goal"], "恢复 Token 刷新行为")
        self.assertEqual(task["revision"], 1)


class CheckRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TempProject()
        self.temp.init_git()
        self.temp.init()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def capture_with_checks(self, checks: list[str]) -> tuple[dict, object]:
        task = self.temp.add_task(checks=checks)
        path = self.temp.write_context(CONTEXT)
        result = self.temp.run(
            "capture",
            "--task",
            task["task_id"],
            "--context",
            str(path),
            "--project",
            str(self.temp.project),
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return self.temp.json_stdout(result)["data"], result

    def test_all_checks_must_run_and_pass(self) -> None:
        command = (
            f"{shlex.quote(sys.executable)} -c \"print(''.join(map(chr, "
            "[76,69,65,75,95,83,69,78,84,73,78,69,76])))\""
        )
        receipt, result = self.capture_with_checks([command])

        self.assertEqual(receipt["auto_checked"]["status"], "passed")
        check = receipt["auto_checked"]["checks"][0]
        self.assertEqual(check["argv"][:2], [sys.executable, "-c"])
        self.assertEqual(check["cwd"], str(self.temp.project.resolve()))
        self.assertEqual(check["returncode"], 0)
        self.assertNotIn("LEAK_SENTINEL", result.stdout)

    def test_nonzero_check_is_failed_even_when_another_check_passes(self) -> None:
        passed = f"{shlex.quote(sys.executable)} -c \"pass\""
        failed = f"{shlex.quote(sys.executable)} -c \"import sys; sys.exit(7)\""
        receipt, _ = self.capture_with_checks([passed, failed])

        self.assertEqual(receipt["auto_checked"]["status"], "failed")
        self.assertEqual([item["status"] for item in receipt["auto_checked"]["checks"]], ["passed", "failed"])

    def test_spawn_error_makes_checks_unavailable(self) -> None:
        receipt, _ = self.capture_with_checks(["offwork-command-that-does-not-exist"])

        self.assertEqual(receipt["auto_checked"]["status"], "unavailable")
        self.assertEqual(receipt["auto_checked"]["checks"][0]["status"], "unavailable")

    def test_timeout_makes_checks_unavailable(self) -> None:
        command = f"{shlex.quote(sys.executable)} -c \"import time; time.sleep(5)\""
        receipt, _ = self.capture_with_checks([command])

        self.assertEqual(receipt["auto_checked"]["status"], "unavailable")
        self.assertEqual(receipt["auto_checked"]["checks"][0]["status"], "unavailable")

    def test_snapshot_is_collected_after_check_changes_project(self) -> None:
        command = (
            f"{shlex.quote(sys.executable)} -c \"from pathlib import Path; "
            "Path('tracked.txt').write_text('changed by check\\n')\""
        )
        receipt, _ = self.capture_with_checks([command])

        self.assertEqual(receipt["auto_checked"]["status"], "passed")
        self.assertEqual(receipt["workspace_freshness"]["status"], "fresh")


class CapsuleIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TempProject()
        self.temp.init_git()
        self.temp.init()
        self.task = self.temp.add_task()
        context = self.temp.write_context(CONTEXT)
        result = self.temp.run(
            "capture", "--task", self.task["task_id"], "--context", str(context),
            "--project", str(self.temp.project), "--json"
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.receipt = self.temp.json_stdout(result)["data"]
        self.capsule_id = self.receipt["capsule"]["capsule_id"]
        self.capsule_dir = self.temp.project / ".offwork" / "capsules" / self.capsule_id

    def tearDown(self) -> None:
        self.temp.cleanup()

    def show(self):
        return self.temp.run(
            "task", "show", self.task["task_id"], "--capsule", self.capsule_id,
            "--project", str(self.temp.project), "--json"
        )

    def test_manifest_tamper_returns_integrity_failure_and_skips_freshness(self) -> None:
        manifest = self.capsule_dir / "manifest.json"
        manifest.write_text(manifest.read_text(encoding="utf-8") + " ", encoding="utf-8")

        result = self.show()

        self.assertNotEqual(result.returncode, 0)
        envelope = self.temp.json_stdout(result)
        self.assertEqual(envelope["error"]["code"], "CAPSULE_INTEGRITY_FAILED")
        self.assertEqual(envelope["error"]["details"]["integrity"], "failed")
        self.assertEqual(envelope["error"]["details"]["freshness"], "not_evaluated")

    def test_payload_tamper_returns_integrity_failure(self) -> None:
        payload = self.capsule_dir / "capsule.json"
        value = json.loads(payload.read_text(encoding="utf-8"))
        value["context"]["summary"] = "tampered"
        payload.write_text(json.dumps(value), encoding="utf-8")

        result = self.show()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self.temp.json_stdout(result)["error"]["code"],
            "CAPSULE_INTEGRITY_FAILED",
        )

    def test_symlinked_payload_is_rejected(self) -> None:
        payload = self.capsule_dir / "checks.json"
        outside = self.temp.root / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        payload.unlink()
        payload.symlink_to(outside)

        result = self.show()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self.temp.json_stdout(result)["error"]["code"],
            "CAPSULE_INTEGRITY_FAILED",
        )

    def test_member_read_uses_bytes_from_same_validated_descriptor(self) -> None:
        member = self.temp.root / "member.json"
        replacement = self.temp.root / "replacement.json"
        original_bytes = b'{"source":"opened descriptor"}'
        member.write_bytes(original_bytes)
        replacement.write_bytes(b'{"source":"replacement path"}')
        os.chmod(member, 0o600)
        os.chmod(replacement, 0o600)
        real_fstat = os.fstat

        def replace_path_after_fstat(descriptor: int):
            current = real_fstat(descriptor)
            member.unlink()
            member.symlink_to(replacement)
            return current

        with mock.patch(
            "offwork.capsule.os.fstat",
            side_effect=replace_path_after_fstat,
        ):
            loaded = _read_private_member(member, "capsule-test", "member.json")

        self.assertEqual(loaded, original_bytes)

    def test_unknown_capsule_member_is_rejected(self) -> None:
        (self.capsule_dir / "extra.txt").write_text("unexpected", encoding="utf-8")

        result = self.show()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self.temp.json_stdout(result)["error"]["code"],
            "CAPSULE_INTEGRITY_FAILED",
        )

    def test_valid_orphan_capsule_is_reconciled_idempotently(self) -> None:
        database = self.temp.project / ".offwork" / "state.sqlite3"
        with sqlite3.connect(str(database)) as connection:
            connection.execute("DELETE FROM capsules WHERE capsule_id = ?", (self.capsule_id,))
            connection.execute(
                "UPDATE tasks SET revision = 1, current_capsule_id = NULL WHERE task_id = ?",
                (self.task["task_id"],),
            )

        first = self.temp.run(
            "task", "show", self.task["task_id"], "--project", str(self.temp.project), "--json"
        )
        second = self.temp.run(
            "task", "show", self.task["task_id"], "--project", str(self.temp.project), "--json"
        )

        self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
        self.assertEqual(second.returncode, 0, second.stderr or second.stdout)
        self.assertEqual(self.temp.json_stdout(first)["data"]["capsule"]["capsule_id"], self.capsule_id)
        with sqlite3.connect(str(database)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM capsules WHERE capsule_id = ?", (self.capsule_id,)
            ).fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
