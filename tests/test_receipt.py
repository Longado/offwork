from __future__ import annotations

import unittest
import subprocess

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


if __name__ == "__main__":
    unittest.main()
