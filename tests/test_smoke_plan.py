"""The live-host plan fixture is isolated and cannot claim success before work."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/agy-codex/scripts"
sys.path.insert(0, str(SCRIPTS))
import smoke


class SmokePlanTests(unittest.TestCase):
    def test_two_step_fixture_starts_pending_with_real_failing_acceptance_tests(self):
        with tempfile.TemporaryDirectory(prefix="agy-codex-plan-smoke-") as directory:
            repo = Path(directory) / "new repo"
            controller, plan = smoke.setup(repo, plan_mode=True)
            self.assertEqual([row["key"] for row in plan["tasks"]], ["T-01", "T-02"])
            self.assertEqual(plan["tasks"][1]["task"]["depends_on"], [plan["tasks"][0]["ticket_id"]])
            self.assertFalse(controller.ticket_dir(plan["tasks"][0]["ticket_id"]).exists())
            result = smoke.verify_plan(repo)
            self.assertFalse(result["passed"])
            self.assertEqual(result["plan"]["completed"], 0)
            self.assertEqual(result["agy_review_recorded_per_task"], [False, False])
            checks = subprocess.run([sys.executable, "-B", "-m", "unittest", "test_clamp", "test_clamp_many"],
                                    cwd=repo, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(checks.returncode, 0)
            self.assertIn("Ran 10 tests", checks.stderr)
            self.assertNotIn("test_clamp.py", plan["tasks"][0]["task"]["allowed_paths"])
            self.assertNotIn("test_clamp_many.py", plan["tasks"][1]["task"]["allowed_paths"])
            with self.assertRaises(smoke.WorkflowError):
                smoke.setup(repo, plan_mode=True)


if __name__ == "__main__":
    unittest.main()
