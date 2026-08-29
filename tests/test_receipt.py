from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import threading
import unittest
from copy import deepcopy
from unittest import mock

from offwork import capsule as capsule_module
from offwork.capsule import capture
from offwork.cli import main
from offwork.output import render_receipt
from offwork.project import load_project
from offwork.state import StateService
from tests.helpers import TempProject
from tests.test_capsule import CONTEXT


class ReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TempProject()
        self.temp.init()
        self.task = self.temp.add_task()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def capture(self, context: dict, name: str) -> dict:
        path = self.temp.write_context(context, name)
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
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return self.temp.json_stdout(result)["data"]

    def test_resume_reloads_published_capsule_and_preserves_context(self) -> None:
        captured = self.capture(CONTEXT, "first.json")

        result = self.temp.run(
            "resume",
            "--task",
            self.task["task_id"],
            "--capsule",
            captured["capsule"]["capsule_id"],
            "--project",
            str(self.temp.project),
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        resumed = self.temp.json_stdout(result)["data"]
        self.assertEqual(resumed["capsule"], captured["capsule"])
        self.assertEqual(resumed["agent_claimed"], captured["agent_claimed"])
        self.assertEqual(resumed["unknowns"], CONTEXT["unknowns"])
        self.assertEqual(resumed["open_loops"], CONTEXT["open_loops"])
        self.assertEqual(resumed["next_step"], CONTEXT["next_step"])

    def test_show_can_address_older_capsule_while_default_resolves_latest(self) -> None:
        first = self.capture(CONTEXT, "first.json")
        second_context = dict(CONTEXT)
        second_context["summary"] = "第二次交接"
        second = self.capture(second_context, "second.json")

        old_result = self.temp.run(
            "task",
            "show",
            self.task["task_id"],
            "--capsule",
            first["capsule"]["capsule_id"],
            "--project",
            str(self.temp.project),
            "--json",
        )
        latest_result = self.temp.run(
            "task",
            "show",
            self.task["task_id"],
            "--project",
            str(self.temp.project),
            "--json",
        )

        self.assertEqual(old_result.returncode, 0, old_result.stderr or old_result.stdout)
        self.assertEqual(latest_result.returncode, 0, latest_result.stderr or latest_result.stdout)
        old = self.temp.json_stdout(old_result)["data"]
        latest = self.temp.json_stdout(latest_result)["data"]
        self.assertEqual(old["capsule"]["capsule_id"], first["capsule"]["capsule_id"])
        self.assertEqual(latest["capsule"]["capsule_id"], second["capsule"]["capsule_id"])
        self.assertEqual(latest["agent_claimed"]["summary"], "第二次交接")

    def test_human_receipt_renders_same_required_facts(self) -> None:
        captured = self.capture(CONTEXT, "human.json")
        result = self.temp.run(
            "task",
            "show",
            self.task["task_id"],
            "--capsule",
            captured["capsule"]["capsule_id"],
            "--project",
            str(self.temp.project),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        for expected in (
            captured["task"]["title"],
            captured["capsule"]["capsule_id"],
            CONTEXT["summary"],
            CONTEXT["unknowns"][0],
            CONTEXT["open_loops"][0]["title"],
            CONTEXT["next_step"],
            "not_run",
            "pending",
            "unavailable",
        ):
            self.assertIn(str(expected), result.stdout)

    def test_human_receipt_renders_all_decision_grade_canonical_facts(self) -> None:
        context = deepcopy(CONTEXT)
        context["open_loops"] = [
            {
                "title": "LOOP_TITLE_SENTINEL",
                "disposition": "delegate",
                "note": "LOOP_NOTE_SENTINEL",
            }
        ]
        captured = self.capture(context, "decision-grade.json")
        receipt = deepcopy(captured)
        receipt["human_acceptance"] = {
            "status": "accepted",
            "acted_at": "ACCEPTED_AT_SENTINEL",
            "note": "ACCEPTANCE_NOTE_SENTINEL",
        }
        receipt["workspace_freshness"].update(
            {
                "scope": "FRESHNESS_SCOPE_SENTINEL",
                "checked_at": "FRESHNESS_CHECKED_AT_SENTINEL",
                "changes": ["FRESHNESS_CHANGE_SENTINEL"],
                "limitations": ["FRESHNESS_LIMITATION_SENTINEL"],
            }
        )
        receipt["auto_checked"] = {
            "status": "failed",
            "checks": [
                {
                    "command": "CHECK_COMMAND_SENTINEL",
                    "argv": ["CHECK_ARGV_SENTINEL", "参数"],
                    "cwd": "CHECK_CWD_SENTINEL",
                    "status": "failed",
                    "returncode": 17,
                    "started_at": "CHECK_STARTED_AT_SENTINEL",
                    "finished_at": "CHECK_FINISHED_AT_SENTINEL",
                }
            ],
        }

        human = render_receipt(receipt)

        expected_facts = (
            receipt["task"]["task_id"],
            receipt["task"]["current_revision"],
            receipt["task"]["captured_revision"],
            receipt["capsule"]["capsule_id"],
            receipt["capsule"]["captured_at"],
            "LOOP_TITLE_SENTINEL",
            "delegate",
            "LOOP_NOTE_SENTINEL",
            "FRESHNESS_SCOPE_SENTINEL",
            "FRESHNESS_CHECKED_AT_SENTINEL",
            "FRESHNESS_CHANGE_SENTINEL",
            "FRESHNESS_LIMITATION_SENTINEL",
            "accepted",
            "ACCEPTED_AT_SENTINEL",
            "ACCEPTANCE_NOTE_SENTINEL",
            "CHECK_COMMAND_SENTINEL",
            "CHECK_ARGV_SENTINEL",
            "参数",
            "CHECK_CWD_SENTINEL",
            "failed",
            17,
            "CHECK_STARTED_AT_SENTINEL",
            "CHECK_FINISHED_AT_SENTINEL",
        )
        for fact in expected_facts:
            self.assertIn(str(fact), human)

    def test_human_receipt_escapes_terminal_controls_but_preserves_chinese(self) -> None:
        captured = self.capture(CONTEXT, "hostile.json")
        receipt = deepcopy(captured)
        receipt["task"]["title"] = (
            "正常中文\n伪造标题\x00\x1b[31m红色\x85"
            "\u202e反向\u2066隔离\ufeff格式\u2028行分隔\u2029段分隔"
        )

        human = render_receipt(receipt)

        self.assertIn("正常中文", human)
        for control in ("\x00", "\x1b", "\x85", "\u202e", "\u2066", "\ufeff", "\u2028", "\u2029"):
            self.assertNotIn(control, human)
        for escaped in (
            "\\n",
            "\\x00",
            "\\x1b",
            "\\u0085",
            "\\u202e",
            "\\u2066",
            "\\ufeff",
            "\\u2028",
            "\\u2029",
        ):
            self.assertIn(escaped, human)

    def test_human_receipt_renders_json_null_facts_as_null(self) -> None:
        captured = self.capture(CONTEXT, "nulls.json")

        human = render_receipt(captured)

        self.assertIn("- Acted at: null", human)
        self.assertIn("- Note: null", human)

    def test_human_receipt_makes_empty_decision_lists_explicit(self) -> None:
        context = deepcopy(CONTEXT)
        context["unknowns"] = []
        context["open_loops"] = []
        captured = self.capture(context, "empty-lists.json")

        human = render_receipt(captured)

        self.assertIn("- Captured changes: none", human)
        self.assertIn("Unknowns:\n- none", human)
        self.assertIn("Open loops:\n- none", human)


class FreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TempProject()
        self.temp.init_git()
        self.temp.init()
        self.task = self.temp.add_task()
        context = self.temp.write_context(CONTEXT)
        result = self.temp.run(
            "capture",
            "--task",
            self.task["task_id"],
            "--context",
            str(context),
            "--project",
            str(self.temp.project),
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.receipt = self.temp.json_stdout(result)["data"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def show(self) -> dict:
        result = self.temp.run(
            "task",
            "show",
            self.task["task_id"],
            "--capsule",
            self.receipt["capsule"]["capsule_id"],
            "--project",
            str(self.temp.project),
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return self.temp.json_stdout(result)["data"]

    def test_fresh_immediately_after_capture(self) -> None:
        self.assertEqual(self.receipt["workspace_freshness"]["status"], "fresh")
        self.assertEqual(self.show()["workspace_freshness"]["status"], "fresh")

    def test_project_change_does_not_change_capsule_integrity(self) -> None:
        (self.temp.project / "tracked.txt").write_text("changed later\n", encoding="utf-8")

        shown = self.show()

        self.assertEqual(shown["handoff_verified"]["integrity"]["status"], "passed")
        self.assertEqual(shown["workspace_freshness"]["status"], "changed")
        self.assertIn("tracked.txt", shown["workspace_freshness"]["changes"])


class InertInspectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TempProject()
        self.temp.init_git()
        self.temp.init()
        self.task = self.temp.add_task()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_capture_show_and_resume_do_not_invoke_repository_fsmonitor(self) -> None:
        sentinel = self.temp.root / "fsmonitor-invoked"
        monitor = self.temp.root / "fsmonitor-sentinel"
        monitor.write_text(
            "#!/bin/sh\n"
            'touch "$(dirname "$0")/fsmonitor-invoked"\n',
            encoding="utf-8",
        )
        monitor.chmod(0o700)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.temp.project),
                "config",
                "core.fsmonitor",
                str(monitor),
            ],
            check=True,
        )
        context = self.temp.write_context(CONTEXT)

        captured_result = self.temp.run(
            "capture",
            "--task",
            self.task["task_id"],
            "--context",
            str(context),
            "--project",
            str(self.temp.project),
            "--json",
        )
        self.assertEqual(
            captured_result.returncode,
            0,
            captured_result.stderr or captured_result.stdout,
        )
        capsule_id = self.temp.json_stdout(captured_result)["data"]["capsule"]["capsule_id"]
        for command in (
            ("task", "show", self.task["task_id"], "--capsule", capsule_id),
            ("resume", "--task", self.task["task_id"], "--capsule", capsule_id),
        ):
            result = self.temp.run(
                *command,
                "--project",
                str(self.temp.project),
                "--json",
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

        self.assertFalse(sentinel.exists(), "receipt inspection invoked core.fsmonitor")

    def test_intermediate_directory_symlink_makes_freshness_unavailable(self) -> None:
        tracked_directory = self.temp.project / "dir"
        tracked_directory.mkdir()
        (tracked_directory / "file").write_text("captured\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.temp.project), "add", "dir/file"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.temp.project), "commit", "-qm", "add nested file"],
            check=True,
        )
        context = self.temp.write_context(CONTEXT)
        captured_result = self.temp.run(
            "capture",
            "--task",
            self.task["task_id"],
            "--context",
            str(context),
            "--project",
            str(self.temp.project),
            "--json",
        )
        self.assertEqual(
            captured_result.returncode,
            0,
            captured_result.stderr or captured_result.stdout,
        )
        captured = self.temp.json_stdout(captured_result)["data"]
        outside_directory = self.temp.root / "outside"
        outside_directory.mkdir()
        (outside_directory / "file").write_text("captured\n", encoding="utf-8")
        shutil.rmtree(tracked_directory)
        tracked_directory.symlink_to(outside_directory, target_is_directory=True)

        shown_result = self.temp.run(
            "task",
            "show",
            self.task["task_id"],
            "--capsule",
            captured["capsule"]["capsule_id"],
            "--project",
            str(self.temp.project),
            "--json",
        )

        self.assertEqual(shown_result.returncode, 0, shown_result.stderr or shown_result.stdout)
        freshness = self.temp.json_stdout(shown_result)["data"]["workspace_freshness"]
        self.assertEqual(freshness["status"], "unavailable")
        self.assertIn("unsafe_workspace_path", freshness["limitations"])

    def test_final_component_symlink_target_remains_part_of_snapshot(self) -> None:
        link = self.temp.project / "tracked-link"
        link.symlink_to("first-target")
        subprocess.run(
            ["git", "-C", str(self.temp.project), "add", "tracked-link"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.temp.project), "commit", "-qm", "add symlink"],
            check=True,
        )
        context = self.temp.write_context(CONTEXT)
        captured_result = self.temp.run(
            "capture",
            "--task",
            self.task["task_id"],
            "--context",
            str(context),
            "--project",
            str(self.temp.project),
            "--json",
        )
        self.assertEqual(
            captured_result.returncode,
            0,
            captured_result.stderr or captured_result.stdout,
        )
        captured = self.temp.json_stdout(captured_result)["data"]
        link.unlink()
        link.symlink_to("second-target")

        shown_result = self.temp.run(
            "task",
            "show",
            self.task["task_id"],
            "--capsule",
            captured["capsule"]["capsule_id"],
            "--project",
            str(self.temp.project),
            "--json",
        )

        self.assertEqual(shown_result.returncode, 0, shown_result.stderr or shown_result.stdout)
        freshness = self.temp.json_stdout(shown_result)["data"]["workspace_freshness"]
        self.assertEqual(freshness["status"], "changed")
        self.assertIn("tracked-link", freshness["changes"])


class NestedProjectFreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TempProject()
        parent = self.temp.project
        subprocess.run(["git", "init", "-q", str(parent)], check=True)
        subprocess.run(["git", "-C", str(parent), "config", "user.email", "offwork@example.test"], check=True)
        subprocess.run(["git", "-C", str(parent), "config", "user.name", "Offwork Tests"], check=True)
        self.nested = parent / "nested"
        self.nested.mkdir()
        (parent / "outside.txt").write_text("outside\n", encoding="utf-8")
        (self.nested / "inside.txt").write_text("inside\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(parent), "add", "outside.txt", "nested/inside.txt"], check=True)
        subprocess.run(["git", "-C", str(parent), "commit", "-qm", "initial"], check=True)
        result = self.temp.run("init", "--project", str(self.nested), "--json")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        result = self.temp.run(
            "task", "add", "nested task", "--goal", "stay bounded",
            "--project", str(self.nested), "--json"
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.task = self.temp.json_stdout(result)["data"]
        context = self.temp.write_context(CONTEXT)
        result = self.temp.run(
            "capture", "--task", self.task["task_id"], "--context", str(context),
            "--project", str(self.nested), "--json"
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.receipt = self.temp.json_stdout(result)["data"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def show_status(self) -> str:
        result = self.temp.run(
            "task", "show", self.task["task_id"], "--capsule", self.receipt["capsule"]["capsule_id"],
            "--project", str(self.nested), "--json"
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return self.temp.json_stdout(result)["data"]["workspace_freshness"]["status"]

    def test_parent_only_dirty_change_is_ignored(self) -> None:
        (self.temp.project / "outside.txt").write_text("dirty outside\n", encoding="utf-8")
        self.assertEqual(self.show_status(), "fresh")

    def test_parent_only_commit_is_ignored(self) -> None:
        (self.temp.project / "outside.txt").write_text("committed outside\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.temp.project), "add", "outside.txt"], check=True)
        subprocess.run(["git", "-C", str(self.temp.project), "commit", "-qm", "outside only"], check=True)
        self.assertEqual(self.show_status(), "fresh")

    def test_show_and_resume_do_not_update_parent_repository_index(self) -> None:
        subprocess.run(
            [
                "git",
                "-C",
                str(self.temp.project),
                "config",
                "core.untrackedCache",
                "true",
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.temp.project), "status", "--porcelain"],
            check=True,
            stdout=subprocess.PIPE,
        )
        index = self.temp.project / ".git" / "index"

        commands = (
            (
                "task",
                "show",
                self.task["task_id"],
                "--capsule",
                self.receipt["capsule"]["capsule_id"],
            ),
            (
                "resume",
                "--task",
                self.task["task_id"],
                "--capsule",
                self.receipt["capsule"]["capsule_id"],
            ),
        )
        for number, command in enumerate(commands):
            probe = self.nested / f"untracked-{number}"
            probe.mkdir()
            (probe / "file.txt").write_text("untracked\n", encoding="utf-8")
            before_stat = index.stat()
            before = (
                index.read_bytes(),
                before_stat.st_mtime_ns,
                before_stat.st_size,
                before_stat.st_ino,
            )

            result = self.temp.run(
                *command,
                "--project",
                str(self.nested),
                "--json",
            )

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            after_stat = index.stat()
            after = (
                index.read_bytes(),
                after_stat.st_mtime_ns,
                after_stat.st_size,
                after_stat.st_ino,
            )
            self.assertEqual(after, before)


class HumanAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TempProject()
        self.temp.init_git()
        self.temp.init()
        self.task = self.temp.add_task(checks=["git status --short"])
        context = self.temp.write_context(CONTEXT)
        result = self.temp.run(
            "capture", "--task", self.task["task_id"], "--context", str(context),
            "--project", str(self.temp.project), "--json"
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.receipt = self.temp.json_stdout(result)["data"]
        self.capsule_id = self.receipt["capsule"]["capsule_id"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def decide(self, action: str, revision: int, note: str = "reviewed"):
        return self.temp.run(
            "task", action, self.task["task_id"], "--capsule", self.capsule_id,
            "--if-revision", str(revision), "--note", note,
            "--project", str(self.temp.project), "--json"
        )

    def show(self) -> dict:
        result = self.temp.run(
            "task", "show", self.task["task_id"], "--capsule", self.capsule_id,
            "--project", str(self.temp.project), "--json"
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return self.temp.json_stdout(result)["data"]

    def stored_acceptance_state(self) -> tuple[int, int, str]:
        database = self.temp.project / ".offwork" / "state.sqlite3"
        with sqlite3.connect(str(database)) as connection:
            revision = connection.execute(
                "SELECT revision FROM tasks WHERE task_id = ?", (self.task["task_id"],)
            ).fetchone()[0]
            events = connection.execute(
                """
                SELECT status FROM human_acceptance_events
                WHERE capsule_id = ? ORDER BY task_revision
                """,
                (self.capsule_id,),
            ).fetchall()
        current = events[-1][0] if events else "pending"
        return revision, len(events), current

    def stored_handoff_state(self):
        database = self.temp.project / ".offwork" / "state.sqlite3"
        with sqlite3.connect(str(database)) as connection:
            task_row = connection.execute(
                "SELECT revision, current_capsule_id FROM tasks WHERE task_id = ?",
                (self.task["task_id"],),
            ).fetchone()
            acceptances = connection.execute(
                "SELECT capsule_id, status, task_revision FROM human_acceptance_events "
                "ORDER BY task_revision"
            ).fetchall()
            registrations = connection.execute(
                "SELECT capsule_id, captured_task_revision FROM capsules "
                "WHERE task_id = ? ORDER BY captured_task_revision, capsule_id",
                (self.task["task_id"],),
            ).fetchall()
        return task_row, acceptances, registrations

    def prepare_pending_candidates(self, captured_revisions: list[int]) -> list[str]:
        capsule_ids = []
        for number in range(len(captured_revisions)):
            context = dict(CONTEXT)
            context["summary"] = f"published candidate {number}"
            context_path = self.temp.write_context(context, f"candidate-{number}.json")
            result = self.temp.run(
                "capture", "--task", self.task["task_id"], "--context", str(context_path),
                "--project", str(self.temp.project), "--json"
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            capsule_ids.append(
                self.temp.json_stdout(result)["data"]["capsule"]["capsule_id"]
            )

        capsules = self.temp.project / ".offwork" / "capsules"
        for capsule_id, captured_revision in zip(capsule_ids, captured_revisions):
            payload_path = capsules / capsule_id / "capsule.json"
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            payload["task"]["captured_revision"] = captured_revision
            payload_bytes = (
                json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            ).encode()
            payload_path.write_bytes(payload_bytes)
            manifest_path = capsules / capsule_id / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"]["capsule.json"] = {
                "size": len(payload_bytes),
                "sha256": hashlib.sha256(payload_bytes).hexdigest(),
            }
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )

        database = self.temp.project / ".offwork" / "state.sqlite3"
        with sqlite3.connect(str(database)) as connection:
            connection.executemany(
                "DELETE FROM capsules WHERE capsule_id = ?",
                [(capsule_id,) for capsule_id in capsule_ids],
            )
            connection.execute(
                "UPDATE tasks SET revision = 2, current_capsule_id = ? WHERE task_id = ?",
                (self.capsule_id, self.task["task_id"]),
            )
        return capsule_ids

    def test_successful_check_does_not_accept_capsule(self) -> None:
        self.assertEqual(self.receipt["auto_checked"]["status"], "passed")
        self.assertEqual(self.receipt["human_acceptance"]["status"], "pending")

    def test_explicit_accept_records_time_note_and_revision(self) -> None:
        result = self.decide("accept", self.receipt["task"]["current_revision"], "looks good")

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        receipt = self.temp.json_stdout(result)["data"]
        self.assertEqual(receipt["human_acceptance"]["status"], "accepted")
        self.assertEqual(receipt["human_acceptance"]["note"], "looks good")
        self.assertIsNotNone(receipt["human_acceptance"]["acted_at"])
        self.assertEqual(receipt["task"]["current_revision"], 3)

    def test_explicit_reject_records_rejected(self) -> None:
        result = self.decide("reject", self.receipt["task"]["current_revision"], "needs work")

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(
            self.temp.json_stdout(result)["data"]["human_acceptance"]["status"],
            "rejected",
        )

    def test_stale_revision_changes_nothing(self) -> None:
        accepted = self.decide("accept", self.receipt["task"]["current_revision"])
        self.assertEqual(accepted.returncode, 0, accepted.stderr or accepted.stdout)

        stale = self.decide("reject", self.receipt["task"]["current_revision"])

        self.assertNotEqual(stale.returncode, 0)
        self.assertEqual(
            self.temp.json_stdout(stale)["error"]["code"], "TASK_REVISION_CONFLICT"
        )
        self.assertEqual(self.show()["human_acceptance"]["status"], "accepted")

    def test_later_explicit_decision_can_replace_current_status(self) -> None:
        accepted = self.decide("accept", self.receipt["task"]["current_revision"])
        accepted_receipt = self.temp.json_stdout(accepted)["data"]
        rejected = self.decide("reject", accepted_receipt["task"]["current_revision"])

        self.assertEqual(rejected.returncode, 0, rejected.stderr or rejected.stdout)
        receipt = self.temp.json_stdout(rejected)["data"]
        self.assertEqual(receipt["human_acceptance"]["status"], "rejected")
        self.assertEqual(receipt["task"]["current_revision"], 4)

    def test_capsule_from_another_task_is_rejected(self) -> None:
        other = self.temp.add_task(title="other", goal="other goal")
        result = self.temp.run(
            "task", "accept", other["task_id"], "--capsule", self.capsule_id,
            "--if-revision", str(other["revision"]), "--project", str(self.temp.project), "--json"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self.temp.json_stdout(result)["error"]["code"], "CAPSULE_TASK_MISMATCH"
        )

    def test_accept_tampered_manifest_changes_no_acceptance_state(self) -> None:
        before = self.stored_acceptance_state()
        manifest = (
            self.temp.project / ".offwork" / "capsules" / self.capsule_id / "manifest.json"
        )
        manifest.write_text(manifest.read_text(encoding="utf-8") + " ", encoding="utf-8")

        result = self.decide("accept", self.receipt["task"]["current_revision"])

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self.temp.json_stdout(result)["error"]["code"],
            "CAPSULE_INTEGRITY_FAILED",
        )
        self.assertEqual(self.stored_acceptance_state(), before)

    def test_accept_tampered_target_does_not_reconcile_valid_orphan(self) -> None:
        second_context = dict(CONTEXT)
        second_context["summary"] = "published but not registered"
        context_path = self.temp.write_context(second_context, "orphan.json")
        captured = self.temp.run(
            "capture", "--task", self.task["task_id"], "--context", str(context_path),
            "--project", str(self.temp.project), "--json"
        )
        self.assertEqual(captured.returncode, 0, captured.stderr or captured.stdout)
        orphan_id = self.temp.json_stdout(captured)["data"]["capsule"]["capsule_id"]
        database = self.temp.project / ".offwork" / "state.sqlite3"
        with sqlite3.connect(str(database)) as connection:
            connection.execute("DELETE FROM capsules WHERE capsule_id = ?", (orphan_id,))
            connection.execute(
                "UPDATE tasks SET revision = 2, current_capsule_id = ? WHERE task_id = ?",
                (self.capsule_id, self.task["task_id"]),
            )

        manifest = (
            self.temp.project / ".offwork" / "capsules" / self.capsule_id / "manifest.json"
        )
        manifest.write_text(manifest.read_text(encoding="utf-8") + " ", encoding="utf-8")

        def stored_state():
            with sqlite3.connect(str(database)) as connection:
                task_row = connection.execute(
                    "SELECT revision, current_capsule_id FROM tasks WHERE task_id = ?",
                    (self.task["task_id"],),
                ).fetchone()
                acceptances = connection.execute(
                    "SELECT capsule_id, status, task_revision FROM human_acceptance_events "
                    "ORDER BY task_revision"
                ).fetchall()
                registrations = connection.execute(
                    "SELECT capsule_id, captured_task_revision FROM capsules "
                    "WHERE task_id = ? ORDER BY captured_task_revision",
                    (self.task["task_id"],),
                ).fetchall()
            return task_row, acceptances, registrations

        before = stored_state()
        result = self.decide("accept", 2)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self.temp.json_stdout(result)["error"]["code"],
            "CAPSULE_INTEGRITY_FAILED",
        )
        self.assertEqual(stored_state(), before)

    def test_accept_requires_pending_exact_next_reconciliation_first(self) -> None:
        orphan_id = self.prepare_pending_candidates([3])[0]
        before = self.stored_handoff_state()

        blocked = self.decide("accept", 2)

        self.assertNotEqual(blocked.returncode, 0)
        error = self.temp.json_stdout(blocked)["error"]
        self.assertEqual(error["code"], "CAPSULE_RECONCILIATION_REQUIRED")
        self.assertEqual(error["details"]["reconciliation_status"], "exact_next")
        self.assertEqual(error["details"]["candidate_capsule_ids"], [orphan_id])
        self.assertEqual(self.stored_handoff_state(), before)

        shown = self.temp.run(
            "task", "show", self.task["task_id"],
            "--project", str(self.temp.project), "--json"
        )
        self.assertEqual(shown.returncode, 0, shown.stderr or shown.stdout)
        shown_receipt = self.temp.json_stdout(shown)["data"]
        self.assertEqual(shown_receipt["capsule"]["capsule_id"], orphan_id)
        self.assertEqual(shown_receipt["task"]["current_revision"], 3)

        accepted = self.decide("accept", 3)

        self.assertEqual(accepted.returncode, 0, accepted.stderr or accepted.stdout)
        accepted_receipt = self.temp.json_stdout(accepted)["data"]
        self.assertEqual(accepted_receipt["capsule"]["capsule_id"], self.capsule_id)
        self.assertEqual(accepted_receipt["task"]["current_revision"], 4)
        self.assertEqual(accepted_receipt["human_acceptance"]["status"], "accepted")

    def test_accept_requires_pending_ambiguous_reconciliation_first(self) -> None:
        candidate_ids = self.prepare_pending_candidates([3, 3])
        before = self.stored_handoff_state()

        result = self.decide("accept", 2)

        self.assertNotEqual(result.returncode, 0)
        error = self.temp.json_stdout(result)["error"]
        self.assertEqual(error["code"], "CAPSULE_RECONCILIATION_REQUIRED")
        self.assertEqual(error["details"]["reconciliation_status"], "ambiguous")
        self.assertEqual(
            error["details"]["candidate_capsule_ids"], sorted(candidate_ids)
        )
        self.assertEqual(self.stored_handoff_state(), before)

    def test_accept_requires_pending_gap_reconciliation_first(self) -> None:
        candidate_id = self.prepare_pending_candidates([4])[0]
        before = self.stored_handoff_state()

        result = self.decide("accept", 2)

        self.assertNotEqual(result.returncode, 0)
        error = self.temp.json_stdout(result)["error"]
        self.assertEqual(error["code"], "CAPSULE_RECONCILIATION_REQUIRED")
        self.assertEqual(error["details"]["reconciliation_status"], "gap")
        self.assertEqual(error["details"]["candidate_capsule_ids"], [candidate_id])
        self.assertEqual(self.stored_handoff_state(), before)

    def test_concurrent_capture_cannot_publish_between_acceptance_plan_and_cas(self) -> None:
        project = load_project(str(self.temp.project))
        context_path = self.temp.write_context(CONTEXT, "concurrent-capture.json")
        plan_complete = threading.Event()
        capture_at_registration = threading.Event()
        acceptance_complete = threading.Event()
        acceptance_result = []
        capture_errors = []
        stdout = io.StringIO()
        real_require = capsule_module._require_no_pending_capsule_reconciliation_locked
        real_register = StateService.register_capsule

        def pause_after_plan(project_value, task_id):
            real_require(project_value, task_id)
            plan_complete.set()
            capture_at_registration.wait(timeout=2)

        def pause_before_registration(service, **arguments):
            capture_at_registration.set()
            if not acceptance_complete.wait(timeout=5):
                raise AssertionError("acceptance did not complete")
            return real_register(service, **arguments)

        def accept_capsule() -> None:
            try:
                acceptance_result.append(
                    main(
                        [
                            "task", "accept", self.task["task_id"],
                            "--capsule", self.capsule_id,
                            "--if-revision", "2",
                            "--project", str(self.temp.project),
                            "--json",
                        ]
                    )
                )
            finally:
                acceptance_complete.set()

        def capture_capsule() -> None:
            try:
                capture(project, self.task["task_id"], str(context_path))
            except Exception as exc:
                capture_errors.append(exc)

        with mock.patch(
            "offwork.cli._require_no_pending_capsule_reconciliation_locked",
            side_effect=pause_after_plan,
        ), mock.patch.object(
            StateService, "register_capsule", autospec=True,
            side_effect=pause_before_registration,
        ), contextlib.redirect_stdout(stdout):
            acceptance_thread = threading.Thread(target=accept_capsule)
            acceptance_thread.start()
            self.assertTrue(plan_complete.wait(timeout=5))
            capture_thread = threading.Thread(target=capture_capsule)
            capture_thread.start()
            acceptance_thread.join(timeout=10)
            capture_thread.join(timeout=10)

        self.assertFalse(acceptance_thread.is_alive())
        self.assertFalse(capture_thread.is_alive())
        self.assertEqual(acceptance_result, [0])
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
        registered_ids = {row[0] for row in registrations}
        published_ids = {
            directory.name
            for directory in (self.temp.project / ".offwork" / "capsules").iterdir()
            if directory.name.startswith("capsule-")
        }
        self.assertEqual(registered_ids, published_ids)
        self.assertEqual(capture_errors, [])
        self.assertEqual(task_row[0], 4)
        self.assertNotEqual(task_row[1], self.capsule_id)
        shown = self.temp.run(
            "task", "show", self.task["task_id"],
            "--project", str(self.temp.project), "--json"
        )
        self.assertEqual(shown.returncode, 0, shown.stderr or shown.stdout)
        self.assertEqual(
            self.temp.json_stdout(shown)["data"]["capsule"]["capsule_id"],
            task_row[1],
        )

    def test_reject_tampered_payload_changes_no_acceptance_state(self) -> None:
        before = self.stored_acceptance_state()
        payload = (
            self.temp.project / ".offwork" / "capsules" / self.capsule_id / "capsule.json"
        )
        value = json.loads(payload.read_text(encoding="utf-8"))
        value["context"]["summary"] = "tampered"
        payload.write_text(json.dumps(value), encoding="utf-8")

        result = self.decide("reject", self.receipt["task"]["current_revision"])

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self.temp.json_stdout(result)["error"]["code"],
            "CAPSULE_INTEGRITY_FAILED",
        )
        self.assertEqual(self.stored_acceptance_state(), before)

if __name__ == "__main__":
    unittest.main()
