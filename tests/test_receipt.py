from __future__ import annotations

import json
import sqlite3
import subprocess
import unittest

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
