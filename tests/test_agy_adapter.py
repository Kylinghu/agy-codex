"""Injected-host contract tests, explicitly not an AGY application test."""
import ast
import asyncio
import json
from pathlib import Path
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]/"skills/agy-codex/scripts"
sys.path.insert(0, str(SCRIPTS))
import agy_adapter
import test_protocol


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.f = test_protocol.ProtocolTests("test_pass_has_independent_tests_diff_and_persistent_session")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.create()
        self.factory_options = []
        self.prompts = []
        self.logs = []

    def run_adapter(self, reviewer, **extra):
        def factory(**options):
            self.factory_options.append(options)
            return reviewer
        args = {"ticket": self.f.ticket_id}
        args.update(extra.pop("workflow_args", {}))
        return asyncio.run(agy_adapter.run_workflow(
            agent_fn=factory, phase_fn=lambda value: None, log_fn=self.logs.append,
            workflow_args=args, controller=self.f.controller,
            preflight=extra.pop("preflight", False)))

    def test_adapter_revise_resume_pass_uses_one_read_only_reviewer(self):
        async def reviewer(prompt, schema):
            self.prompts.append(prompt)
            packet = json.loads(prompt.split("\n\nREVIEW_PACKET\n", 1)[1])
            self.assertEqual(schema["properties"]["verdict"]["enum"], ["PASS", "REVISE", "ESCALATE"])
            self.assertIsNone(self.f.controller.load(self.f.ticket_id)["child_pid"])
            review = self.f.review("REVISE" if len(self.prompts) == 1 else "PASS", packet)
            return review
        result = self.run_adapter(reviewer)
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["round"], 2)
        self.assertEqual(
            self.factory_options,
            [{"role": "AGY read-only reviewer", "type_name": "agy-codex-reviewer", "workspace": "inherit"}],
        )
        self.assertEqual(len(self.f.calls()), 2)
        self.assertTrue(self.f.calls()[1]["resume"])
        self.assertIn("F-001", self.prompts[1])

    def test_reviewer_type_configurable_and_self_blocked_by_default(self):
        async def reviewer(prompt, schema):
            return self.f.review()

        blocked = self.run_adapter(reviewer, workflow_args={"reviewer_type": "self"})
        self.assertEqual(blocked["status"], "REVIEW")
        self.assertIn("self", blocked["review_error"])
        self.assertEqual(len(self.f.calls()), 1)

        custom = self.run_adapter(reviewer, workflow_args={"reviewer_type": "custom-ro-reviewer"})
        self.assertEqual(custom["status"], "PASS", custom)
        self.assertEqual(self.factory_options[-1]["type_name"], "custom-ro-reviewer")
        self.assertEqual(len(self.f.calls()), 1)

    def test_self_reviewer_explicit_opt_in_marks_non_independent_source(self):
        async def reviewer(prompt, schema):
            return self.f.review()

        result = self.run_adapter(
            reviewer,
            workflow_args={"reviewer_type": "self", "allow_non_independent_self_review": True},
        )
        self.assertEqual(result["status"], "PASS", result)
        state = self.f.controller.load(self.f.ticket_id)
        self.assertEqual(state["last_review"]["reviewer_source"], "agy-self-non-independent")

    def test_missing_reviewer_factory_error_preserves_review_without_rerunning_worker(self):
        def missing_factory(**options):
            raise RuntimeError("Subagent type not registered: agy-codex-reviewer")
        result = asyncio.run(agy_adapter.run_workflow(
            agent_fn=missing_factory, phase_fn=lambda value: None, log_fn=self.logs.append,
            workflow_args={"ticket": self.f.ticket_id}, controller=self.f.controller, preflight=False))
        self.assertEqual(result["status"], "REVIEW")
        self.assertIn("not registered", result["review_error"])
        self.assertEqual(len(self.f.calls()), 1)

    def test_reviewer_error_preserves_pending_review_without_rerunning_worker(self):
        async def unavailable(prompt, schema):
            raise RuntimeError("Injected host review failure")
        result = self.run_adapter(unavailable)
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(len(self.f.calls()), 1)
        async def available(prompt, schema):
            return self.f.review()
        result = self.run_adapter(available)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(len(self.f.calls()), 1)

    def test_read_only_violation_invalidates_returned_pass(self):
        async def bad_reviewer(prompt, schema):
            review = self.f.review()
            (self.f.repo/"value.py").write_text("VALUE = 54321\n", encoding="utf-8")
            return review
        result = self.run_adapter(bad_reviewer)
        self.assertEqual(result["status"], "REVIEW")
        self.assertIn("stale", result["review_error"])

    def test_host_script_compiles_with_top_level_await_and_has_no_print_calls(self):
        path = SCRIPTS/"agy_workflow.py"
        source = path.read_text(encoding="utf-8")
        compile(source, str(path), "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        nodes = ast.walk(ast.parse(source))
        self.assertFalse(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print" for n in nodes))


if __name__ == "__main__":
    unittest.main()
