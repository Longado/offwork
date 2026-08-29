from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
