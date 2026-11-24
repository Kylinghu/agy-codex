"""Serial plan behavior over real subprocess fixtures; no live model calls."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/agy-codex/scripts"
sys.path.insert(0, str(SCRIPTS))
import agy_adapter
import plans
import test_protocol


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.f = test_protocol.ProtocolTests("test_pass_has_independent_tests_diff_and_persistent_session")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.c = self.f.controller
        self.logs, self.phases, self.reviewers, self.packets = [], [], [], []
        self.decisions = []

    def task(self, number=1):
        return {"title": f"Fixture step {number}", "goal": f"Complete fixture step {number} with a positive VALUE.",
                "acceptance": ["VALUE is positive"], "allowed_paths": ["value.py"],
                "test_commands": [["{python}", "-c", "from value import VALUE; assert VALUE > 0"]]}

    def plan(self, count=2):
        rows = []
        for number in range(1, count + 1):
            task = self.task(number)
            task["depends_on"] = [f"T-{number - 1:02d}"] if number > 1 else []
            rows.append({"key": f"T-{number:02d}", "task": task})
        return {"title": "Agreed fixture plan", "tasks": rows}

    def factory(self, **options):
        self.reviewers.append(options)
        async def reviewer(prompt, schema):
            packet = json.loads(prompt.split("\n\nREVIEW_PACKET\n", 1)[1])
            self.packets.append(packet)
            self.f.ticket_id = packet["ticket_id"]
            choice = self.decisions.pop(0) if self.decisions else "PASS"
            if isinstance(choice, Exception):
                raise choice
            review = self.f.review(choice if isinstance(choice, str) else "ESCALATE", packet)
            if isinstance(choice, dict):
                review["blocker"] = choice
            return review
        return reviewer

    async def invoke_async(self, options=None, factory=None):
        return await agy_adapter.run_workflow(
            agent_fn=factory or self.factory, phase_fn=self.phases.append, log_fn=self.logs.append,
            workflow_args=options or {"action": "continue"}, controller=self.c, preflight=False)

    def invoke(self, options=None):
        return asyncio.run(self.invoke_async(options))

    def test_one_workflow_runs_two_tickets_with_fresh_sessions_and_successor_baseline(self):
        head = self.f.git("rev-parse", "HEAD")
        result = self.invoke({"action": "start", "plan": self.plan()})
        self.assertEqual((result["status"], result["completed"], result["verified"]), ("PASS", 2, True))
        self.assertEqual(len(self.reviewers), 2)
        calls = self.f.calls()
        self.assertEqual([call["resume"] for call in calls], [False, False])
        self.assertNotEqual(calls[0]["returned_session"], calls[1]["returned_session"])
        second = result["tasks"][1]["ticket_id"]
        self.assertEqual((self.c.ticket_dir(second) / "baseline-files/value.py").read_text(), "VALUE = 1\n")
        diff = Path(self.packets[1]["diff_path"]).read_text()
        self.assertIn("-VALUE = 1", diff)
        self.assertIn("+VALUE = 2", diff)
        self.assertNotIn("-VALUE = 0", diff)
        self.assertEqual(result["usage"]["codex"], {"input_tokens": 24, "output_tokens": 16})
        self.assertEqual(self.f.git("rev-parse", "HEAD"), head)
        self.assertTrue(any(phase.startswith("T-02 · 2/2") for phase in self.phases))

    def test_workflow_start_model_overrides_plan_and_task_by_field(self):
        value = self.plan()
        value["codex"] = {"model": "plan-default", "reasoning_effort": "medium"}
        value["tasks"][0]["task"]["codex"] = {"model": "task-first", "reasoning_effort": "low"}
        result = self.invoke({"action": "start", "plan": value,
                              "codex": {"model": "workflow-model"}})
        self.assertTrue(result["verified"])
        saved = plans.load_plan(self.c, result["plan_id"])
        expected = [{"model": "workflow-model", "reasoning_effort": "low"},
                    {"model": "workflow-model", "reasoning_effort": "medium"}]
        for entry, call, settings in zip(saved["tasks"], self.f.calls(), expected):
            self.assertEqual(entry["task"]["codex"], settings)
            self.assertEqual(self.c.load(entry["ticket_id"])["codex_requested"], settings)
            args = call["argv"]
            self.assertEqual(args[args.index("-m") + 1], settings["model"])
            self.assertEqual(args[args.index("-c") + 1],
                             "model_reasoning_effort=" + json.dumps(settings["reasoning_effort"]))
        self.assertEqual(len(self.f.calls()), 2)

    def test_continue_model_upgrade_does_not_leak_to_next_plan_step(self):
        value = self.plan()
        value["codex"] = {"model": "economy-model", "reasoning_effort": "low"}
        value["tasks"][0]["task"]["codex"] = {"model": "initial-model"}
        # Leave the first Ticket in REVIEW, with a saved worker Session.
        self.decisions = [RuntimeError("Injected offline reviewer failure")]
        result = self.invoke({"action": "start", "plan": value})
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(len(self.f.calls()), 1)
        first_id = result["current"]["ticket_id"]
        self.c = self.f.new_controller()
        self.f.controller = self.c
        # Two repairs of the first Ticket retain its explicit upgrade.
        self.decisions = ["REVISE", "REVISE", "PASS", "PASS"]
        result = self.invoke({"action": "continue", "ticket": first_id,
                              "codex": {"model": "flagship-model", "reasoning_effort": "high"}})
        self.assertTrue(result["verified"])
        calls = self.f.calls()
        self.assertEqual(len(calls), 4)
        self.assertEqual([x["resume"] for x in calls], [False, True, True, False])
        self.assertEqual([x["argv"][x["argv"].index("-m") + 1] for x in calls],
                         ["initial-model", "flagship-model", "flagship-model", "economy-model"])
        self.assertEqual([x["argv"][x["argv"].index("-c") + 1] for x in calls],
                         ['model_reasoning_effort="low"', 'model_reasoning_effort="high"',
                          'model_reasoning_effort="high"', 'model_reasoning_effort="low"'])
        self.assertEqual(calls[1]["requested_session"], calls[0]["returned_session"])
        self.assertEqual(calls[2]["requested_session"], calls[0]["returned_session"])
        self.assertNotEqual(calls[3]["returned_session"], calls[0]["returned_session"])
        first, second = [self.c.load(row["ticket_id"]) for row in result["tasks"]]
        self.assertEqual(first["round"], 3)
        self.assertEqual(first["spec"]["codex"]["model"], "initial-model")
        self.assertEqual(first["codex_requested"]["model"], "flagship-model")
        self.assertEqual(second["codex_requested"], value["codex"])

    def test_repair_reuses_only_current_session_and_reviewer(self):
        self.decisions = ["REVISE", "PASS", "PASS"]
        result = self.invoke({"action": "start", "plan": self.plan()})
        self.assertTrue(result["verified"])
        self.assertEqual([row["round"] for row in result["tasks"]], [2, 1])
        self.assertEqual(len(self.reviewers), 2)
        calls = self.f.calls()
        self.assertEqual([call["resume"] for call in calls], [False, True, False])
        self.assertEqual(calls[1]["requested_session"], calls[0]["returned_session"])
        self.assertNotEqual(calls[2]["returned_session"], calls[0]["returned_session"])
        self.assertEqual([packet["review_mode"] for packet in self.packets], ["full", "followup", "full"])

    def test_three_revisions_stop_plan_and_repeated_start_does_not_reset_budget(self):
        value = self.plan()
        self.decisions = ["REVISE"] * 3
        result = self.invoke({"action": "start", "plan": value})
        self.assertEqual(result["status"], "ESCALATE")
        self.assertEqual([row["status"] for row in result["tasks"]], ["ESCALATE", "PENDING"])
        result = self.invoke({"action": "start", "plan": value})
        self.assertEqual(result["tasks"][0]["round"], 3)
        self.assertEqual(len(self.f.calls()), 3)
        self.assertFalse(self.c.ticket_dir(result["tasks"][1]["ticket_id"]).exists())

    def test_real_product_question_stops_then_resolution_resumes_and_advances(self):
        self.decisions = ["PASS", {"kind": "product_decision", "detail": "Choose the fixture behavior.",
                                  "evidence": "Two requested behaviors conflict.", "question": "Use the positive value?",
                                  "recommendation": "Use the positive value."}]
        result = self.invoke({"action": "start", "plan": self.plan(3)})
        self.assertEqual((result["completed"], result["status"]), (1, "ESCALATE"))
        self.assertEqual(len(self.f.calls()), 2)
        self.assertEqual(result["tasks"][2]["status"], "PENDING")
        self.invoke()
        self.assertEqual(len(self.f.calls()), 2)
        result = self.invoke({"action": "resolve", "resolution": {
            "answer": "Use the positive value.", "source": "user", "round": 1}})
        self.assertTrue(result["verified"])
        calls = self.f.calls()
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[2]["requested_session"], calls[1]["returned_session"])

    def test_pending_review_continues_in_new_controller_without_reimplementation(self):
        self.decisions = [RuntimeError("Injected offline reviewer failure")]
        result = self.invoke({"action": "start", "plan": self.plan()})
        self.assertEqual(result["status"], "REVIEW")
        self.c = self.f.new_controller()
        self.f.controller = self.c
        result = self.invoke({"action": "continue", "ticket": result["current"]["ticket_id"]})
        self.assertTrue(result["verified"])
        self.assertEqual(len(self.f.calls()), 2)
        self.assertEqual([row["round"] for row in result["tasks"]], [1, 1])

    def test_pass_before_cursor_crash_recovers_from_ticket_state(self):
        with mock.patch.object(plans, "checkpoint", side_effect=OSError("Injected cursor publication failure")):
            with self.assertRaisesRegex(OSError, "publication"):
                self.invoke({"action": "start", "plan": self.plan()})
        saved = plans.load_plan(self.c)
        self.assertEqual(saved["cursor"], 0)
        self.assertEqual(plans.position(self.c, saved)[0], 1)
        self.c = self.f.new_controller()
        self.f.controller = self.c
        result = self.invoke()
        self.assertTrue(result["verified"])
        self.assertEqual(len(self.f.calls()), 2)

    def test_external_edit_between_steps_prevents_successor_creation(self):
        original = plans.checkpoint
        def changed(controller, plan):
            original(controller, plan)
            (self.f.repo / "value.py").write_text("VALUE = 1000\n", encoding="utf-8")
        with mock.patch.object(plans, "checkpoint", side_effect=changed):
            result = self.invoke({"action": "start", "plan": self.plan()})
        self.assertEqual((result["status"], result["completed"]), ("BLOCKED", 1))
        self.assertIn("between plan steps", result["message"])
        self.assertEqual(len(self.f.calls()), 1)
        self.assertFalse(self.c.ticket_dir(result["tasks"][1]["ticket_id"]).exists())
        self.assertEqual((self.f.repo / "value.py").read_text(), "VALUE = 1000\n")

    def test_worker_can_update_plan_document_without_changing_frozen_contracts(self):
        source = self.f.repo / "PLAN.md"
        source.write_text("# Agreed fixture plan\nT-01 and T-02 remain to do.\n", encoding="utf-8")
        initial_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        value = self.plan()
        value["plan_ref"] = {"path": "PLAN.md"}
        for row in value["tasks"]:
            row["task"]["allowed_paths"].append("PLAN.md")
        self.f.mode("success", update_plan=True)
        result = self.invoke({"action": "start", "plan": value})
        self.assertTrue(result["verified"])
        first, second = [self.c.load(row["ticket_id"]) for row in result["tasks"]]
        self.assertEqual(first["spec"]["plan_ref"]["sha256_at_start"], initial_hash)
        self.assertNotEqual(second["spec"]["plan_ref"]["sha256_at_start"], initial_hash)
        self.assertEqual(second["spec"]["plan_ref"]["section"], "T-02")
        self.assertTrue(self.invoke({"action": "start", "plan": value})["verified"])
        self.assertEqual(len(self.f.calls()), 2)

    def test_external_edit_after_last_verdict_is_reported_as_blocked_not_current_pass(self):
        original = self.c.verdict
        def changed(*args, **kwargs):
            state = original(*args, **kwargs)
            (self.f.repo / "value.py").write_text("VALUE = 1000\n", encoding="utf-8")
            return state
        with mock.patch.object(self.c, "verdict", side_effect=changed):
            result = self.invoke({"action": "start", "plan": self.plan(1)})
        self.assertEqual((result["status"], result["completed"], result["verified"]), ("BLOCKED", 1, False))
        self.assertEqual(self.invoke({"action": "status"})["status"], "BLOCKED")
        self.assertEqual(len(self.f.calls()), 1)

    def test_manifest_without_pointer_is_found_read_only_then_continued(self):
        saved = plans.start_plan(self.c, self.plan())
        pointer = self.c.root / "active-plan.json"
        pointer.unlink()
        manifest = plans.plan_path(self.c, saved["id"])
        before = manifest.read_bytes()
        status = self.invoke({"action": "status"})
        self.assertEqual(status["plan_id"], saved["id"])
        self.assertEqual(manifest.read_bytes(), before)
        self.assertFalse(pointer.exists())
        self.assertFalse(self.c.ticket_dir(saved["tasks"][0]["ticket_id"]).exists())
        self.decisions = [RuntimeError("Injected offline reviewer failure")]
        self.invoke({"action": "continue", "plan_id": saved["id"]})
        self.assertEqual(json.loads(pointer.read_text())["plan_id"], saved["id"])
        result = self.invoke()
        self.assertTrue(result["verified"])
        self.assertEqual(len(self.f.calls()), 2)

    def test_workflow_lock_spans_review_but_status_remains_read_only(self):
        async def scenario():
            entered, release = asyncio.Event(), asyncio.Event()
            def factory(**options):
                regular = self.factory(**options)
                async def reviewer(prompt, schema):
                    if not entered.is_set():
                        entered.set()
                        await release.wait()
                    return await regular(prompt, schema)
                return reviewer
            running = asyncio.create_task(self.invoke_async({"action": "start", "plan": self.plan()}, factory))
            try:
                await asyncio.wait_for(entered.wait(), 10)
                saved = plans.load_plan(self.c)
                path = plans.plan_path(self.c, saved["id"])
                before = path.read_bytes()
                result = await self.invoke_async({"action": "status"})
                self.assertEqual(result["status"], "REVIEW")
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(self.c.ticket_dir(saved["tasks"][1]["ticket_id"]).exists())
                with self.assertRaisesRegex(test_protocol.sa.WorkflowError, "running controller"):
                    await self.invoke_async()
            finally:
                release.set()
                result = await running
            self.assertTrue(result["verified"])
        asyncio.run(scenario())

    def test_new_manifest_recovers_when_pointer_still_names_old_completed_plan(self):
        first = self.invoke({"action": "start", "plan": self.plan(1)})
        pointer = self.c.root / "active-plan.json"
        original = plans.atomic_json
        def fail_pointer(path, value):
            if path == pointer:
                raise OSError("Injected pointer publication failure")
            return original(path, value)
        with mock.patch.object(plans, "atomic_json", side_effect=fail_pointer):
            with self.assertRaisesRegex(OSError, "pointer publication"):
                self.invoke({"action": "start", "plan": self.plan(2)})
        self.assertEqual(json.loads(pointer.read_text())["plan_id"], first["plan_id"])
        result = self.invoke()
        self.assertTrue(result["verified"])
        self.assertNotEqual(result["plan_id"], first["plan_id"])
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(self.f.calls()), 3)

    def test_invalid_plan_contracts_are_rejected_before_any_worker(self):
        malformed = []
        duplicate = self.plan()
        duplicate["tasks"][1]["key"] = "T-01"
        malformed.append(duplicate)
        for dependency in ("T-01", "T-02"):
            value = self.plan()
            value["tasks"][0]["task"]["depends_on"] = [dependency]
            malformed.append(value)
        for ref in (None, "PLAN.md", {}):
            value = self.plan()
            value["tasks"][0]["task"]["plan_ref"] = ref
            malformed.append(value)
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(test_protocol.sa.WorkflowError):
                self.invoke({"action": "start", "plan": copy.deepcopy(value)})
        self.assertEqual(self.f.calls(), [])
        self.assertFalse((self.c.root / "active-plan.json").exists())

    def test_active_single_ticket_cannot_be_hidden_by_starting_a_plan(self):
        self.f.create()
        with self.assertRaisesRegex(test_protocol.sa.WorkflowError, "Worktree is reserved"):
            self.invoke({"action": "start", "plan": self.plan()})
        self.assertEqual(self.f.calls(), [])

    def test_completed_plan_does_not_hide_explicit_ticket_or_replace_new_active_task(self):
        value = self.plan(1)
        self.invoke({"action": "start", "plan": value})
        self.f.ticket_id = "SINGLE-NEW"
        self.f.create(title="A newer single task")
        with self.assertRaisesRegex(test_protocol.sa.WorkflowError, "Worktree is reserved"):
            self.invoke({"action": "start", "plan": value})
        status = self.invoke({"action": "status"})
        self.assertEqual(status["ticket_id"], "SINGLE-NEW")
        result = self.invoke({"action": "continue", "ticket": "SINGLE-NEW"})
        self.assertTrue(result["verified"])
        self.assertEqual(result["ticket_id"], "SINGLE-NEW")
        self.assertEqual(len(self.f.calls()), 2)

    def test_stopping_paused_plan_preserves_evidence_and_allows_new_task(self):
        self.decisions = [RuntimeError("Injected offline reviewer failure")]
        result = self.invoke({"action": "start", "plan": self.plan()})
        ticket_id = result["current"]["ticket_id"]
        evidence = self.c.ticket_dir(ticket_id) / "rounds/01/codex.jsonl"
        before = evidence.read_bytes()
        result = self.invoke({"action": "stop_plan", "reason": "User changed priorities."})
        self.assertEqual(result["status"], "STOPPED")
        self.assertEqual(self.invoke()["status"], "STOPPED")
        self.assertEqual(evidence.read_bytes(), before)
        self.assertEqual(len(self.f.calls()), 1)
        result = self.invoke({"action": "start", "task": self.task(99)})
        self.assertTrue(result["verified"])
        self.assertEqual(len(self.f.calls()), 2)


if __name__ == "__main__":
    unittest.main()
