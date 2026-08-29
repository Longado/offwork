from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shlex
import sqlite3
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

from offwork.capsule import _read_private_member, reconcile_capsules
from offwork.cli import main
from offwork.errors import OffworkError
from offwork.project import load_project
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

    def capture_another(self, name: str) -> dict:
        context = dict(CONTEXT)
        context["summary"] = name
        context_path = self.temp.write_context(context, f"{name}.json")
        result = self.temp.run(
            "capture", "--task", self.task["task_id"], "--context", str(context_path),
            "--project", str(self.temp.project), "--json"
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return self.temp.json_stdout(result)["data"]

    def orphan_capsule(self, capsule_id: str, revision: int, current_capsule_id: str) -> None:
        database = self.temp.project / ".offwork" / "state.sqlite3"
        with sqlite3.connect(str(database)) as connection:
            connection.execute("DELETE FROM capsules WHERE capsule_id = ?", (capsule_id,))
            connection.execute(
                "UPDATE tasks SET revision = ?, current_capsule_id = ? WHERE task_id = ?",
                (revision, current_capsule_id, self.task["task_id"]),
            )

    def rewrite_capsule_payload(self, capsule_id: str, change) -> None:
        directory = self.temp.project / ".offwork" / "capsules" / capsule_id
        payload_path = directory / "capsule.json"
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        change(payload)
        self.replace_capsule_payload(capsule_id, payload)

    def replace_capsule_payload(self, capsule_id: str, payload) -> str:
        directory = self.temp.project / ".offwork" / "capsules" / capsule_id
        payload_path = directory / "capsule.json"
        payload_bytes = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode()
        payload_path.write_bytes(payload_bytes)

        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"]["capsule.json"] = {
            "size": len(payload_bytes),
            "sha256": hashlib.sha256(payload_bytes).hexdigest(),
        }
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode()
        manifest_path.write_bytes(manifest_bytes)
        return hashlib.sha256(manifest_bytes).hexdigest()

    def replace_manifest(self, capsule_id: str, manifest) -> str:
        manifest_path = (
            self.temp.project / ".offwork" / "capsules" / capsule_id / "manifest.json"
        )
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode()
        manifest_path.write_bytes(manifest_bytes)
        return hashlib.sha256(manifest_bytes).hexdigest()

    def read_manifest(self, capsule_id: str) -> dict:
        manifest_path = (
            self.temp.project / ".offwork" / "capsules" / capsule_id / "manifest.json"
        )
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    def register_manifest_hash(self, capsule_id: str, manifest_hash: str) -> None:
        database = self.temp.project / ".offwork" / "state.sqlite3"
        with sqlite3.connect(str(database)) as connection:
            connection.execute(
                "UPDATE capsules SET manifest_hash = ? WHERE capsule_id = ?",
                (manifest_hash, capsule_id),
            )

    def assert_registered_manifest_integrity_failure(self, manifest) -> None:
        manifest_hash = self.replace_manifest(self.capsule_id, manifest)
        self.register_manifest_hash(self.capsule_id, manifest_hash)

        result = self.show()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self.temp.json_stdout(result)["error"]["code"],
            "CAPSULE_INTEGRITY_FAILED",
        )

    def assert_orphan_manifest_is_ignored(self, manifest) -> None:
        second = self.capture_another("malformed manifest orphan")
        second_capsule_id = second["capsule"]["capsule_id"]
        self.orphan_capsule(second_capsule_id, 2, self.capsule_id)
        self.replace_manifest(second_capsule_id, manifest(second_capsule_id))

        result = self.temp.run(
            "task", "show", self.task["task_id"],
            "--project", str(self.temp.project), "--json"
        )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(
            self.temp.json_stdout(result)["data"]["capsule"]["capsule_id"], self.capsule_id
        )
        database = self.temp.project / ".offwork" / "state.sqlite3"
        with sqlite3.connect(str(database)) as connection:
            task_row = connection.execute(
                "SELECT revision, current_capsule_id FROM tasks WHERE task_id = ?",
                (self.task["task_id"],),
            ).fetchone()
            registered = connection.execute(
                "SELECT COUNT(*) FROM capsules WHERE capsule_id = ?",
                (second_capsule_id,),
            ).fetchone()[0]
        self.assertEqual(task_row, (2, self.capsule_id))
        self.assertEqual(registered, 0)

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

    def test_hash_consistent_non_object_registered_payload_is_integrity_failure(self) -> None:
        manifest_hash = self.replace_capsule_payload(self.capsule_id, [])
        self.register_manifest_hash(self.capsule_id, manifest_hash)

        result = self.show()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self.temp.json_stdout(result)["error"]["code"],
            "CAPSULE_INTEGRITY_FAILED",
        )

    def test_hash_consistent_non_object_registered_manifest_is_integrity_failure(self) -> None:
        self.assert_registered_manifest_integrity_failure([])

    def test_hash_consistent_non_object_registered_manifest_files_is_integrity_failure(self) -> None:
        manifest = self.read_manifest(self.capsule_id)
        manifest["files"] = None
        self.assert_registered_manifest_integrity_failure(manifest)

    def test_hash_consistent_non_object_registered_file_declaration_is_integrity_failure(self) -> None:
        manifest = self.read_manifest(self.capsule_id)
        manifest["files"]["capsule.json"] = []
        self.assert_registered_manifest_integrity_failure(manifest)

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

    def test_later_published_capsule_is_reconciled_without_rewriting_history(self) -> None:
        first_capsule_id = self.capsule_id
        second = self.capture_another("second capture")
        second_capsule_id = second["capsule"]["capsule_id"]
        capsules = self.temp.project / ".offwork" / "capsules"
        before = {
            capsule_id: {
                member.name: (member.read_bytes(), member.stat().st_mtime_ns)
                for member in (capsules / capsule_id).iterdir()
            }
            for capsule_id in (first_capsule_id, second_capsule_id)
        }
        self.orphan_capsule(second_capsule_id, 2, first_capsule_id)

        first_read = self.temp.run(
            "task", "show", self.task["task_id"],
            "--project", str(self.temp.project), "--json"
        )
        second_read = self.temp.run(
            "resume", "--task", self.task["task_id"],
            "--project", str(self.temp.project), "--json"
        )

        self.assertEqual(first_read.returncode, 0, first_read.stderr or first_read.stdout)
        self.assertEqual(second_read.returncode, 0, second_read.stderr or second_read.stdout)
        self.assertEqual(
            self.temp.json_stdout(first_read)["data"]["capsule"]["capsule_id"],
            second_capsule_id,
        )
        self.assertEqual(
            self.temp.json_stdout(second_read)["data"]["task"]["current_revision"], 3
        )
        historical = self.temp.run(
            "task", "show", self.task["task_id"], "--capsule", first_capsule_id,
            "--project", str(self.temp.project), "--json"
        )
        self.assertEqual(historical.returncode, 0, historical.stderr or historical.stdout)
        after = {
            capsule_id: {
                member.name: (member.read_bytes(), member.stat().st_mtime_ns)
                for member in (capsules / capsule_id).iterdir()
            }
            for capsule_id in (first_capsule_id, second_capsule_id)
        }
        self.assertEqual(after, before)
        database = self.temp.project / ".offwork" / "state.sqlite3"
        with sqlite3.connect(str(database)) as connection:
            task_row = connection.execute(
                "SELECT revision, current_capsule_id FROM tasks WHERE task_id = ?",
                (self.task["task_id"],),
            ).fetchone()
            registrations = connection.execute(
                "SELECT capsule_id, captured_task_revision FROM capsules "
                "WHERE task_id = ? ORDER BY captured_task_revision",
                (self.task["task_id"],),
            ).fetchall()
        self.assertEqual(task_row, (3, second_capsule_id))
        self.assertEqual(registrations, [(first_capsule_id, 2), (second_capsule_id, 3)])

    def test_capture_retry_reconciles_existing_orphan_before_publishing_next_revision(self) -> None:
        first_capsule_id = self.capsule_id
        orphan = self.capture_another("interrupted capture")
        orphan_capsule_id = orphan["capsule"]["capsule_id"]
        self.orphan_capsule(orphan_capsule_id, 2, first_capsule_id)

        retry = self.capture_another("capture retry")
        retry_capsule_id = retry["capsule"]["capsule_id"]

        self.assertEqual(retry["task"]["captured_revision"], 4)
        self.assertEqual(retry["task"]["current_revision"], 4)
        database = self.temp.project / ".offwork" / "state.sqlite3"
        with sqlite3.connect(str(database)) as connection:
            task_row = connection.execute(
                "SELECT revision, current_capsule_id FROM tasks WHERE task_id = ?",
                (self.task["task_id"],),
            ).fetchone()
            registrations = connection.execute(
                "SELECT capsule_id, captured_task_revision FROM capsules "
                "WHERE task_id = ? ORDER BY captured_task_revision",
                (self.task["task_id"],),
            ).fetchall()
        self.assertEqual(task_row, (4, retry_capsule_id))
        self.assertEqual(
            registrations,
            [(first_capsule_id, 2), (orphan_capsule_id, 3), (retry_capsule_id, 4)],
        )

    def test_ambiguous_orphans_block_capture_without_publishing_another_capsule(self) -> None:
        first_capsule_id = self.capsule_id
        second = self.capture_another("ambiguous retry one")
        second_capsule_id = second["capsule"]["capsule_id"]
        third = self.capture_another("ambiguous retry two")
        third_capsule_id = third["capsule"]["capsule_id"]
        self.orphan_capsule(second_capsule_id, 2, first_capsule_id)
        self.orphan_capsule(third_capsule_id, 2, first_capsule_id)
        self.rewrite_capsule_payload(
            third_capsule_id,
            lambda payload: payload["task"].update({"captured_revision": 3}),
        )
        capsules = self.temp.project / ".offwork" / "capsules"
        before = sorted(path.name for path in capsules.iterdir())
        context_path = self.temp.write_context(CONTEXT, "blocked-retry.json")

        result = self.temp.run(
            "capture", "--task", self.task["task_id"], "--context", str(context_path),
            "--project", str(self.temp.project), "--json"
        )

        self.assertNotEqual(result.returncode, 0)
        error = self.temp.json_stdout(result)["error"]
        self.assertEqual(error["code"], "CAPSULE_RECONCILIATION_AMBIGUOUS")
        self.assertEqual(
            error["details"]["candidate_capsule_ids"],
            sorted([second_capsule_id, third_capsule_id]),
        )
        self.assertEqual(sorted(path.name for path in capsules.iterdir()), before)

    def test_revision_gap_blocks_capture_without_publishing_another_capsule(self) -> None:
        second = self.capture_another("gap before retry")
        second_capsule_id = second["capsule"]["capsule_id"]
        self.orphan_capsule(second_capsule_id, 2, self.capsule_id)
        self.rewrite_capsule_payload(
            second_capsule_id,
            lambda payload: payload["task"].update({"captured_revision": 4}),
        )
        capsules = self.temp.project / ".offwork" / "capsules"
        before = sorted(path.name for path in capsules.iterdir())
        context_path = self.temp.write_context(CONTEXT, "gap-blocked-retry.json")

        result = self.temp.run(
            "capture", "--task", self.task["task_id"], "--context", str(context_path),
            "--project", str(self.temp.project), "--json"
        )

        self.assertNotEqual(result.returncode, 0)
        error = self.temp.json_stdout(result)["error"]
        self.assertEqual(error["code"], "CAPSULE_RECONCILIATION_GAP")
        self.assertEqual(error["details"]["candidate_capsule_ids"], [second_capsule_id])
        self.assertEqual(sorted(path.name for path in capsules.iterdir()), before)

    def test_ambiguous_next_capsules_fail_closed_without_state_change(self) -> None:
        first_capsule_id = self.capsule_id
        second = self.capture_another("second candidate")
        second_capsule_id = second["capsule"]["capsule_id"]
        third = self.capture_another("other second candidate")
        third_capsule_id = third["capsule"]["capsule_id"]
        self.orphan_capsule(second_capsule_id, 2, first_capsule_id)
        self.orphan_capsule(third_capsule_id, 2, first_capsule_id)
        self.rewrite_capsule_payload(
            third_capsule_id,
            lambda payload: payload["task"].update({"captured_revision": 3}),
        )

        results = [
            self.temp.run(
                "task", "show", self.task["task_id"],
                "--project", str(self.temp.project), "--json"
            )
            for _ in range(2)
        ]

        for result in results:
            self.assertNotEqual(result.returncode, 0)
            error = self.temp.json_stdout(result)["error"]
            self.assertEqual(error["code"], "CAPSULE_RECONCILIATION_AMBIGUOUS")
            self.assertEqual(
                error["details"]["candidate_capsule_ids"],
                sorted([second_capsule_id, third_capsule_id]),
            )
        database = self.temp.project / ".offwork" / "state.sqlite3"
        with sqlite3.connect(str(database)) as connection:
            task_row = connection.execute(
                "SELECT revision, current_capsule_id FROM tasks WHERE task_id = ?",
                (self.task["task_id"],),
            ).fetchone()
            registrations = connection.execute(
                "SELECT capsule_id FROM capsules WHERE task_id = ? ORDER BY capsule_id",
                (self.task["task_id"],),
            ).fetchall()
        self.assertEqual(task_row, (2, first_capsule_id))
        self.assertEqual(registrations, [(first_capsule_id,)])

    def test_concurrent_reconcilers_accept_the_same_winner_idempotently(self) -> None:
        second = self.capture_another("racing second capture")
        second_capsule_id = second["capsule"]["capsule_id"]
        self.orphan_capsule(second_capsule_id, 2, self.capsule_id)
        project = load_project(str(self.temp.project))
        barrier = threading.Barrier(2)
        errors: list[Exception] = []
        from offwork import capsule as capsule_module

        real_verify = capsule_module._verify_directory

        def synchronized_verify(directory, capsule_id, expected_manifest_hash):
            loaded = real_verify(directory, capsule_id, expected_manifest_hash)
            if capsule_id == second_capsule_id and expected_manifest_hash is None:
                barrier.wait(timeout=5)
            return loaded

        def run_reconciler() -> None:
            try:
                reconcile_capsules(project, self.task["task_id"])
            except Exception as exc:  # captured for the main test thread
                errors.append(exc)

        with mock.patch("offwork.capsule._verify_directory", side_effect=synchronized_verify):
            threads = [threading.Thread(target=run_reconciler) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        reconcile_capsules(project, self.task["task_id"])
        database = self.temp.project / ".offwork" / "state.sqlite3"
        with sqlite3.connect(str(database)) as connection:
            task_row = connection.execute(
                "SELECT revision, current_capsule_id FROM tasks WHERE task_id = ?",
                (self.task["task_id"],),
            ).fetchone()
            count = connection.execute(
                "SELECT COUNT(*) FROM capsules WHERE capsule_id = ?",
                (second_capsule_id,),
            ).fetchone()[0]
        self.assertEqual(task_row, (3, second_capsule_id))
        self.assertEqual(count, 1)

    def test_foreign_project_candidate_is_not_reconciled(self) -> None:
        second = self.capture_another("foreign candidate")
        second_capsule_id = second["capsule"]["capsule_id"]
        self.orphan_capsule(second_capsule_id, 2, self.capsule_id)
        self.rewrite_capsule_payload(
            second_capsule_id,
            lambda payload: payload["observed"].update({"project_id": "project-foreign"}),
        )

        result = self.temp.run(
            "task", "show", self.task["task_id"],
            "--project", str(self.temp.project), "--json"
        )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(
            self.temp.json_stdout(result)["data"]["capsule"]["capsule_id"], self.capsule_id
        )
        database = self.temp.project / ".offwork" / "state.sqlite3"
        with sqlite3.connect(str(database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM capsules WHERE capsule_id = ?", (second_capsule_id,)
                ).fetchone()[0],
                0,
            )

    def test_hash_consistent_non_object_orphan_is_ignored_without_state_change(self) -> None:
        second = self.capture_another("malformed orphan")
        second_capsule_id = second["capsule"]["capsule_id"]
        self.orphan_capsule(second_capsule_id, 2, self.capsule_id)
        self.replace_capsule_payload(second_capsule_id, [])

        result = self.temp.run(
            "task", "show", self.task["task_id"],
            "--project", str(self.temp.project), "--json"
        )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(
            self.temp.json_stdout(result)["data"]["capsule"]["capsule_id"], self.capsule_id
        )
        database = self.temp.project / ".offwork" / "state.sqlite3"
        with sqlite3.connect(str(database)) as connection:
            task_row = connection.execute(
                "SELECT revision, current_capsule_id FROM tasks WHERE task_id = ?",
                (self.task["task_id"],),
            ).fetchone()
            registrations = connection.execute(
                "SELECT capsule_id FROM capsules WHERE task_id = ? ORDER BY capsule_id",
                (self.task["task_id"],),
            ).fetchall()
        self.assertEqual(task_row, (2, self.capsule_id))
        self.assertEqual(registrations, [(self.capsule_id,)])

    def test_hash_consistent_non_object_orphan_manifest_is_ignored(self) -> None:
        self.assert_orphan_manifest_is_ignored(lambda _capsule_id: [])

    def test_hash_consistent_non_object_orphan_manifest_files_is_ignored(self) -> None:
        def malformed(capsule_id):
            manifest = self.read_manifest(capsule_id)
            manifest["files"] = None
            return manifest

        self.assert_orphan_manifest_is_ignored(malformed)

    def test_hash_consistent_non_object_orphan_file_declaration_is_ignored(self) -> None:
        def malformed(capsule_id):
            manifest = self.read_manifest(capsule_id)
            manifest["files"]["capsule.json"] = []
            return manifest

        self.assert_orphan_manifest_is_ignored(malformed)

    def test_skipped_revision_candidate_fails_closed(self) -> None:
        second = self.capture_another("skipped candidate")
        second_capsule_id = second["capsule"]["capsule_id"]
        self.orphan_capsule(second_capsule_id, 2, self.capsule_id)
        self.rewrite_capsule_payload(
            second_capsule_id,
            lambda payload: payload["task"].update({"captured_revision": 4}),
        )

        result = self.temp.run(
            "task", "show", self.task["task_id"],
            "--project", str(self.temp.project), "--json"
        )

        self.assertNotEqual(result.returncode, 0)
        error = self.temp.json_stdout(result)["error"]
        self.assertEqual(error["code"], "CAPSULE_RECONCILIATION_GAP")
        self.assertEqual(error["details"]["candidate_capsule_ids"], [second_capsule_id])
        database = self.temp.project / ".offwork" / "state.sqlite3"
        with sqlite3.connect(str(database)) as connection:
            task_row = connection.execute(
                "SELECT revision, current_capsule_id FROM tasks WHERE task_id = ?",
                (self.task["task_id"],),
            ).fetchone()
        self.assertEqual(task_row, (2, self.capsule_id))


if __name__ == "__main__":
    unittest.main()
