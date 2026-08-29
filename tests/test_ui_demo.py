import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
UI_ROOT = ROOT / "demo-ui"


class AcidUiDemoTests(unittest.TestCase):
    def test_demo_declares_a_local_vite_entrypoint(self) -> None:
        self.assertTrue((UI_ROOT / "package.json").exists(), "demo package is missing")
        package = json.loads((UI_ROOT / "package.json").read_text(encoding="utf-8"))

        self.assertEqual(package["scripts"]["dev"], "vite")
        self.assertIn("vite build", package["scripts"]["build"])

    def test_demo_keeps_truth_states_and_not_run_distinct(self) -> None:
        self.assertTrue(
            (UI_ROOT / "src" / "Prototype.jsx").exists(),
            "demo prototype source is missing",
        )
        source = (UI_ROOT / "src" / "Prototype.jsx").read_text(encoding="utf-8")

        for label in (
            "Agent claimed",
            "Offwork observed",
            "Auto checked",
            "Handoff verified",
            "Human acceptance",
        ):
            self.assertIn(label, source)
        self.assertIn("Not run", source)
        self.assertIn("Capsule integrity", source)
        self.assertIn("Workspace freshness", source)

    def test_human_acceptance_requires_explicit_controls(self) -> None:
        self.assertTrue(
            (UI_ROOT / "src" / "Prototype.jsx").exists(),
            "demo prototype source is missing",
        )
        source = (UI_ROOT / "src" / "Prototype.jsx").read_text(encoding="utf-8")

        self.assertIn('setAcceptance("accepted")', source)
        self.assertIn('setAcceptance("rejected")', source)
        self.assertIn('useState("pending")', source)

    def test_storyboard_distinguishes_recorded_runs_from_design_targets(self) -> None:
        storyboard = UI_ROOT / "src" / "Storyboard.jsx"
        self.assertTrue(storyboard.exists(), "desktop story source is missing")
        source = storyboard.read_text(encoding="utf-8")

        self.assertIn("RECORDED RUN", source)
        self.assertIn("DESIGN TARGET", source)
        self.assertIn("History search is not implemented in the CLI yet", source)
        self.assertIn("Pack current work", source)


if __name__ == "__main__":
    unittest.main()
