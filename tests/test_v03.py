"""Meaningful v0.3 regressions: native product decisions and task-start evidence.

All model calls are explicit test doubles; no model or AGY host pass is implied.
"""
import asyncio
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]/"skills/agy-codex/scripts"
sys.path.insert(0, str(SCRIPTS))
import agy_adapter
import product
import test_protocol


class AgyCodexTests(unittest.TestCase):
    def setUp(self):
        self.f = test_protocol.ProtocolTests("test_pass_has_independent_tests_diff_and_persistent_session")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.c = self.f.controller
        self.f.create()
        self.attempts = 0

    def workflow(self, reviewer, **options):
        async def tracked(prompt, schema):
            self.attempts += 1
            return await reviewer(prompt, schema)
        return asyncio.run(agy_adapter.run_workflow(
            agent_fn=lambda **kwargs: tracked, phase_fn=lambda text: None, log_fn=lambda text: None,
            workflow_args={"ticket": self.f.ticket_id, **options}, controller=self.c, preflight=False))

    def escalation(self, kind):
        review = self.f.review("ESCALATE")
        review["findings"] = []
        review["blocker"] = {"kind": kind, "detail": "Concrete fixture blocker", "evidence": "Observed fixture evidence",
                             "question": "Which compatible fixture behavior is intended?", "recommendation": "Keep the existing behavior"}
        return self.c.verdict(self.f.ticket_id, review, source="test")

    def test_routine_choice_is_resolved_by_existing_reviewer_in_same_session(self):
        self.f.mode("decision")
        async def reviewer(prompt, schema):
            if self.attempts == 1:
                packet = self.c.packet(self.f.ticket_id)
                self.assertEqual(packet["worker_report"]["issue"]["kind"], "engineering_choice")
                self.f.mode("success")
                review = self.f.review("REVISE")
                review["findings"][0].update(id="DECISION-01", requested_fix="Use the existing positive integer convention.")
                return review
            return self.f.review()
        result = self.workflow(reviewer)
        self.assertTrue(result["verified"])
        first, second = self.f.calls()
        self.assertEqual(second["requested_session"], first["returned_session"])
        self.assertIn("positive integer convention", second["prompt"])
        self.assertEqual(result["round"], 2)
        self.assertEqual(self.attempts, 2)

    def test_product_choice_requires_real_answer_and_blocks_new_task(self):
        self.f.mode("decision", kind="product_decision")
        initial = self.c.run(self.f.ticket_id)
        contract = self.c.load(self.f.ticket_id)["spec"]
        state = self.escalation("product_decision")
        self.assertTrue(state["resolution_allowed"])
        with self.assertRaisesRegex(RuntimeError, "user's actual answer"):
            self.c.resolve(self.f.ticket_id, "I guessed", "router", expected_round=1)
        with self.assertRaisesRegex(RuntimeError, "current blocked round"):
            self.c.resolve(self.f.ticket_id, "Keep compatible behavior", "user", expected_round=0)
        with self.assertRaisesRegex(RuntimeError, "reserved"):
            self.f.create(id="SECOND")
        self.f.mode("success")
        async def reviewer(prompt, schema):
            return self.f.review()
        result = self.workflow(reviewer, action="resolve", resolution={"round": 1, "answer": "Keep compatible behavior", "source": "user"})
        self.assertTrue(result["verified"])
        self.assertEqual(self.f.calls()[1]["requested_session"], initial["session_id"])
        self.assertEqual(self.c.load(self.f.ticket_id)["spec"], contract)
        self.assertEqual(self.c.load(self.f.ticket_id)["resolutions"][0]["source"], "user")
        with self.assertRaisesRegex(RuntimeError, "cannot be reopened"):
            self.c.resolve(self.f.ticket_id, "duplicate", "user", expected_round=1)
        self.assertEqual(len(self.f.calls()), 2)

    def test_fresh_reviewer_restores_user_resolution_after_another_revision(self):
        self.f.mode("decision", kind="product_decision")
        first = self.c.run(self.f.ticket_id)
        self.escalation("product_decision")
        answer = "Preserve the legacy positive-integer behavior."
        resolved = self.c.resolve(self.f.ticket_id, answer, "user", expected_round=1)
        resolutions = resolved["resolutions"]
        original_spec = resolved["spec"]
        self.f.mode("success")
        self.c.run(self.f.ticket_id)
        self.c.verdict(self.f.ticket_id, self.f.review("REVISE"))
        self.assertNotIn(answer, json.dumps(self.c.load(self.f.ticket_id)["last_review"]))

        # A new host workflow must restore the earlier choice, even though the
        # latest review now contains only an unrelated repair finding.
        self.c = self.f.new_controller()
        self.f.controller = self.c
        packets = []
        async def reviewer(prompt, schema):
            packets.append(json.loads(prompt.split("\n\nREVIEW_PACKET\n", 1)[1]))
            return self.f.review()
        result = self.workflow(reviewer)
        self.assertTrue(result["verified"])
        self.assertEqual(result["round"], 3)
        self.assertEqual(packets[0]["review_mode"], "full")
        self.assertEqual(packets[0].get("resolutions"), resolutions)
        state = self.c.load(self.f.ticket_id)
        saved = json.loads((self.c._round_dir(state) / "review-packet.json").read_text())
        self.assertEqual(saved.get("resolutions"), resolutions)
        self.assertEqual(state["spec"], original_spec)
        self.assertEqual(state["session_id"], first["session_id"])
        self.assertEqual(len(self.f.calls()), 3)

    def test_review_input_restores_resolutions_from_ticket_for_legacy_packets(self):
        self.c.run(self.f.ticket_id)
        self.escalation("product_decision")
        self.c.resolve(self.f.ticket_id, "Keep the compatible public behavior.", "user", expected_round=1)
        state = self.c.run(self.f.ticket_id)
        packet = self.c.packet(self.f.ticket_id)
        packet.pop("resolutions", None)  # Saved by a controller before this field existed.
        for fresh in (True, False):
            with self.subTest(fresh=fresh):
                data = product.review_input(self.c, state, packet, fresh)
                self.assertEqual(data.get("resolutions"), state["resolutions"])
                self.assertEqual(data["review_mode"], "full" if fresh else "followup")
                self.assertEqual("spec" in data, fresh)
        self.assertNotIn("resolutions", packet)

    def test_permission_blocker_is_not_pass_or_code_defect_or_textual_grant(self):
        self.f.mode("permission")
        self.c.run(self.f.ticket_id)
        with self.assertRaisesRegex(RuntimeError, "unresolved worker issue"):
            self.c.verdict(self.f.ticket_id, self.f.review())
        with self.assertRaisesRegex(RuntimeError, "permission blocker"):
            self.c.verdict(self.f.ticket_id, self.f.review("REVISE"))
        self.escalation("permission")
        with self.assertRaisesRegex(RuntimeError, "not a permission grant"):
            self.c.resolve(self.f.ticket_id, "The user authorized everything", "user", expected_round=1)
        state = self.c.load(self.f.ticket_id)
        self.assertEqual(state["status"], "ESCALATE")
        self.assertFalse(product.report(self.c, state)["verified"])
        self.assertEqual(len(self.f.calls()), 1)

    def test_permission_failure_skips_host_validation_and_cannot_be_reclassified(self):
        self.c.escalate(self.f.ticket_id, "Use isolated validation marker fixture")
        self.f.ticket_id = "PERMISSION"
        self.f.create(test_commands=[["{python}", "-c", "from pathlib import Path; Path('.git/validation-was-run').write_text('ran')"]])
        self.f.mode("permission")
        self.c.run(self.f.ticket_id)
        self.assertFalse((self.f.repo/".git/validation-was-run").exists())
        self.assertIn("skipped", self.c.packet(self.f.ticket_id)["tests"][0])
        review = self.f.review("ESCALATE")
        for blocker in (None, {"kind": "product_decision", "detail": "Reclassified issue", "evidence": "User may agree",
                               "question": "May I continue?", "recommendation": "Continue"}):
            review["blocker"] = blocker
            with self.assertRaisesRegex(RuntimeError, "blocker.kind=permission"):
                self.c.verdict(self.f.ticket_id, review)
        state = self.escalation("permission")
        self.assertEqual(state["last_review"]["worker_permission_evidence"], "Explicit test fixture evidence")

    def test_invalid_enum_types_are_reviewable_instead_of_recovery_loops(self):
        self.c.run(self.f.ticket_id)
        path = self.c._round_dir(self.c.load(self.f.ticket_id))/"last-message.md"
        valid = json.loads(path.read_text())
        for malformed in ({**valid, "outcome": []}, {**valid, "outcome": "blocked", "issue": {"kind": {}}}):
            path.write_text(json.dumps(malformed))
            self.c.refresh(self.f.ticket_id)
            self.assertEqual(self.c.packet(self.f.ticket_id)["worker_report"]["outcome"], "invalid_report")
            self.assertFalse(self.c.load(self.f.ticket_id)["recoverable_interruption"])
        self.assertEqual(len(self.f.calls()), 1)

    def test_new_session_failure_is_not_hidden_by_previous_review_summary(self):
        self.c.run(self.f.ticket_id)
        self.c.verdict(self.f.ticket_id, self.f.review("REVISE"))
        self.f.mode("mismatch-resume")
        state = self.c.run(self.f.ticket_id)
        result = product.report(self.c, state)
        self.assertIn("different Session ID", result["summary"])
        self.assertNotEqual(result["summary"], result["review_summary"])

    def test_observed_environment_fix_retains_original_capabilities_and_budget(self):
        self.f.mode("environment")
        first = self.c.run(self.f.ticket_id)
        self.escalation("environment")
        self.f.mode("success")
        self.c.resolve(self.f.ticket_id, "The missing fixture resource is now available", "router",
                       evidence="Fixture resource read succeeded after repair", expected_round=1)
        self.assertEqual(self.c.load(self.f.ticket_id)["round"], 1)
        self.c.run(self.f.ticket_id)
        argv = self.f.calls()[1]["argv"]
        self.assertEqual(argv[:6], ["-a", "never", "-s", "workspace-write", "exec", "resume"])
        self.assertEqual(self.f.calls()[1]["requested_session"], first["session_id"])
        self.assertEqual(self.c.load(self.f.ticket_id)["round"], 2)

    def test_decision_at_third_round_cannot_resolve_into_fourth_invocation(self):
        self.f.mode("decision")
        for number in (1, 2, 3):
            self.c.run(self.f.ticket_id)
            if number < 3:
                self.c.verdict(self.f.ticket_id, self.f.review("REVISE"))
        state = self.escalation("product_decision")
        self.assertFalse(state["resolution_allowed"])
        with self.assertRaisesRegex(RuntimeError, "cannot be reopened"):
            self.c.resolve(self.f.ticket_id, "Proceed", "user", expected_round=3)
        self.assertEqual(len(self.f.calls()), 3)

    def test_hard_escalation_cannot_be_reopened_as_a_decision(self):
        self.c.escalate(self.f.ticket_id, "Unresolved Session provenance")
        with self.assertRaisesRegex(RuntimeError, "cannot be reopened"):
            self.c.resolve(self.f.ticket_id, "Proceed", "user", expected_round=0)
        self.assertEqual(self.f.calls(), [])

    def test_completed_worker_recovery_preserves_unresolved_issue(self):
        self.f.mode("permission")
        with mock.patch.object(self.c, "_prepare_review", side_effect=OSError("Evidence publication interrupted")):
            first = self.c.run(self.f.ticket_id)
        self.assertTrue(first["recoverable_interruption"])
        self.c.recover(self.f.ticket_id)
        self.assertEqual(self.c.packet(self.f.ticket_id)["worker_report"]["outcome"], "blocked")
        with self.assertRaisesRegex(RuntimeError, "unresolved worker issue"):
            self.c.verdict(self.f.ticket_id, self.f.review())
        self.assertEqual(len(self.f.calls()), 1)

    def test_decision_does_not_skip_scope_validation(self):
        self.c.escalate(self.f.ticket_id, "Replace fixture with a disjoint scoped test")
        self.f.ticket_id = "SCOPED"
        self.f.create(allowed_paths=["other.py"])
        self.f.mode("decision")
        state = self.c.run(self.f.ticket_id)
        self.assertEqual(state["status"], "ESCALATE")
        self.assertIn("outside Ticket scope", state["reason"])
        self.assertFalse(state["resolution_allowed"])

    def test_invalid_worker_report_is_never_treated_as_done(self):
        self.f.mode("invalid-report")
        self.c.run(self.f.ticket_id)
        self.assertEqual(self.c.packet(self.f.ticket_id)["worker_report"]["outcome"], "invalid_report")
        with self.assertRaisesRegex(RuntimeError, "invalid report"):
            self.c.verdict(self.f.ticket_id, self.f.review())

    def test_review_format_is_corrected_once_without_another_implementation(self):
        async def reviewer(prompt, schema):
            review = self.f.review()
            if self.attempts == 1:
                del review["axes"]
            else:
                self.assertIn("validator reported", prompt)
            return review
        result = self.workflow(reviewer)
        self.assertTrue(result["verified"])
        self.assertEqual(self.attempts, 2)
        self.assertEqual(len(self.f.calls()), 1)
        self.assertEqual(result["round"], 1)

    def test_second_invalid_review_remains_pending_without_loop(self):
        async def reviewer(prompt, schema):
            return {"verdict": "maybe"}
        result = self.workflow(reviewer)
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(self.attempts, 2)
        self.assertEqual(len(self.f.calls()), 1)

    def test_review_permission_error_is_not_retried(self):
        async def reviewer(prompt, schema):
            raise RuntimeError("Temporary read permission denied")
        result = self.workflow(reviewer)
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(self.attempts, 1)
        self.assertIn("permission denied", result["review_error"])

    def test_second_task_can_start_without_committing_the_first(self):
        head = self.f.git("rev-parse", "HEAD")
        self.c.run(self.f.ticket_id)
        self.c.verdict(self.f.ticket_id, self.f.review())
        self.f.ticket_id = "SECOND"
        self.f.create()
        self.c.run(self.f.ticket_id)
        packet = self.c.packet(self.f.ticket_id)
        patch = Path(packet["diff_path"]).read_text()
        self.assertIn("-VALUE = 1", patch)
        self.assertIn("+VALUE = 2", patch)
        self.assertNotIn("-VALUE = 0", patch)
        self.assertEqual(self.f.git("rev-parse", "HEAD"), head)
        self.assertFalse(self.f.calls()[1]["resume"])


if __name__ == "__main__":
    unittest.main()
