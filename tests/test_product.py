"""Product-boundary tests with real processes and an explicitly fake Codex/AGY."""
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT/"skills/agy-codex/scripts"
sys.path.insert(0, str(SCRIPTS))
import agy_adapter
import product
import test_protocol

install_spec = importlib.util.spec_from_file_location("agy_codex_installer", ROOT/"install.py")
installer = importlib.util.module_from_spec(install_spec)
install_spec.loader.exec_module(installer)


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.f = test_protocol.ProtocolTests("test_pass_has_independent_tests_diff_and_persistent_session")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.c = self.f.controller
        self.task = {"title": "Fix the public fixture value", "goal": "Make VALUE positive without changing unrelated behavior.",
                     "acceptance": ["VALUE is a positive integer"], "allowed_paths": ["value.py"],
                     "test_commands": [["{python}", "-c", "from value import VALUE; assert VALUE > 0"]]}
        self.prompts = []
        self.factories = []
        self.logs = []
        self.decisions = []

    def call(self, options):
        async def reviewer(prompt, schema):
            self.prompts.append(prompt)
            data = json.loads(prompt.split("\n\nREVIEW_PACKET\n", 1)[1])
            self.f.ticket_id = data["ticket_id"]
            decision = self.decisions.pop(0) if self.decisions else "PASS"
            result = self.f.review(decision, data)
            result["axes"] = {"spec": "Independent fixture evidence reviewed.", "standards": "Fixture conventions reviewed."}
            return result
        def factory(**kwargs):
            self.factories.append(kwargs)
            return reviewer
        return asyncio.run(agy_adapter.run_workflow(agent_fn=factory, phase_fn=lambda text: None,
                                                   log_fn=self.logs.append, workflow_args=options,
                                                   controller=self.c, preflight=False))

    def test_start_needs_no_id_or_spec_file_and_runs_integrated_contracts(self):
        result = self.call({"action": "start", "task": self.task})
        self.assertTrue(result["verified"])
        self.assertTrue(result["ticket_id"].startswith("TASK-"))
        self.assertEqual(self.f.git("status", "--porcelain"), "M value.py")
        self.assertTrue((self.c.ticket_dir(result["ticket_id"])/"contract.json").exists())
        self.assertIn("sole agent writer", self.f.calls()[0]["prompt"])
        self.assertIn("## Spec", self.prompts[0])
        self.assertIn("## Standards", self.prompts[0])
        self.assertEqual(self.factories[0]["type_name"], "research")
        self.assertNotIn("session_id", result)

    def test_identical_start_is_idempotent_and_does_not_repeat_worker_or_review(self):
        first = self.call({"action": "start", "task": self.task})
        second = self.call({"action": "start", "task": self.task})
        self.assertEqual(first["ticket_id"], second["ticket_id"])
        self.assertEqual(len(self.f.calls()), 1)
        self.assertEqual(len(self.prompts), 1)

    def test_status_idle_does_not_create_state_or_start_any_agent(self):
        result = self.call({"action": "status"})
        self.assertEqual(result["status"], "IDLE")
        self.assertFalse(self.c.root.exists())
        self.assertEqual(self.f.calls(), [])
        self.assertEqual(self.factories, [])

    def test_status_pending_review_is_read_only(self):
        state = product.start_task(self.c, self.task)
        self.c.run(state["id"])
        before = (self.c.ticket_dir(state["id"])/"ticket.json").read_bytes()
        result = self.call({"action": "status"})
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(len(self.f.calls()), 1)
        self.assertEqual(self.factories, [])
        self.assertEqual(before, (self.c.ticket_dir(state["id"])/"ticket.json").read_bytes())

    def test_repair_reuses_session_and_sends_delta_to_worker_and_live_reviewer(self):
        self.decisions = ["REVISE", "PASS"]
        result = self.call({"action": "start", "task": self.task})
        first, second = self.f.calls()
        self.assertTrue(result["verified"])
        self.assertTrue(second["resume"])
        self.assertEqual(second["requested_session"], first["returned_session"])
        self.assertIn("F-001", second["prompt"])
        self.assertNotIn(self.task["goal"], second["prompt"])
        self.assertLess(len(second["prompt"]), len(first["prompt"]))
        followup = json.loads(self.prompts[1].split("\n\nREVIEW_PACKET\n", 1)[1])
        self.assertEqual(followup["review_mode"], "followup")
        self.assertNotIn("spec", followup)
        self.assertEqual(followup["files_changed_since_review"], ["value.py"])
        self.assertEqual(len(self.factories), 1)
        self.assertEqual(result["usage"]["codex"], {"input_tokens": 24, "output_tokens": 16})
        self.assertIsNone(result["usage"]["agy"])

    def test_continue_recovers_timeout_without_requiring_a_ticket_or_session_id(self):
        state = product.start_task(self.c, self.task)
        failed = self.f.run_with_bound_session_timeout(state["id"])
        self.assertEqual(failed["status"], "ESCALATE")
        self.assertIn("Process timeout", failed["reason"])
        self.assertTrue(failed["recoverable_interruption"])
        self.assertIsNone(failed["child_pid"])
        self.assertFalse(test_protocol.sa.pid_alive(failed["last_child_pid"]))
        original = failed["session_id"]
        self.f.mode("success")
        result = self.call({"action": "continue"})
        self.assertTrue(result["verified"])
        self.assertEqual(self.f.calls()[1]["requested_session"], original)

    def test_continue_does_not_bypass_a_substantive_escalation(self):
        state = product.start_task(self.c, self.task)
        self.c.escalate(state["id"], "Requires a product decision")
        result = self.call({"action": "continue"})
        self.assertEqual(result["status"], "ESCALATE")
        self.assertEqual(self.f.calls(), [])

    def test_continue_recovers_evidence_failure_without_repeating_completed_worker(self):
        state = product.start_task(self.c, self.task)
        with mock.patch.object(self.c, "_prepare_review", side_effect=OSError("Temporary evidence write failure")):
            failed = self.c.run(state["id"])
        self.assertTrue(failed["recoverable_interruption"])
        original = failed["session_id"]
        result = self.call({"action": "continue"})
        self.assertTrue(result["verified"])
        self.assertEqual(result["round"], 1)
        self.assertEqual(self.c.load(state["id"])["session_id"], original)
        self.assertEqual(len(self.f.calls()), 1)

    def test_stale_pending_review_gives_actionable_status_without_execution(self):
        state = product.start_task(self.c, self.task)
        self.c.run(state["id"])
        (self.f.repo/"value.py").write_text("VALUE = 88\n", encoding="utf-8")
        status = self.call({"action": "status"})
        self.assertIn("旧验收不能代表当前文件", status["message"])
        self.assertIn("核对外部改动", status["next_action"])
        continued = self.call({"action": "continue"})
        self.assertEqual(continued["status"], "REVIEW")
        self.assertFalse(continued["snapshot_current"])
        self.assertEqual(len(self.f.calls()), 1)
        self.assertEqual(self.factories, [])

    def test_new_contract_cannot_be_smuggled_into_continue(self):
        product.start_task(self.c, self.task)
        with self.assertRaisesRegex(RuntimeError, "cannot silently replace"):
            self.call({"action": "continue", "task": self.task})
        self.assertEqual(self.f.calls(), [])

    def test_plan_pointer_is_revision_stamped_without_copying_plan_body(self):
        text = "# Existing canonical plan\n\n## T-03\nImplement the fixture.\n"
        on_disk = text.replace("\n", "\r\n").encode("utf-8")
        (self.f.repo/"PLAN.md").write_bytes(on_disk)
        self.f.git("add", "PLAN.md")
        self.f.git("commit", "-qm", "Fixture plan")
        self.task["plan_ref"] = {"path": "PLAN.md", "section": "T-03"}
        state = product.start_task(self.c, self.task)
        self.assertEqual(state["spec"]["plan_ref"]["sha256_at_start"], hashlib.sha256(on_disk).hexdigest())
        self.assertNotIn(text, json.dumps(state))
        self.assertEqual((self.f.repo/"PLAN.md").read_text(), text)

    def test_plan_pointer_preserves_lf_and_crlf_as_distinct_revisions(self):
        hashes = []
        for newline in (b"\n", b"\r\n"):
            with self.subTest(newline=newline):
                content = newline.join((b"# Plan", b"## T-03", b"Implement the fixture.", b""))
                (self.f.repo/"PLAN.md").write_bytes(content)
                ref = product._plan_ref(self.c, {"path": "PLAN.md", "section": "T-03"})
                self.assertEqual(ref["sha256_at_start"], hashlib.sha256(content).hexdigest())
                self.assertEqual((self.f.repo/"PLAN.md").read_bytes(), content)
                hashes.append(ref["sha256_at_start"])
        self.assertNotEqual(*hashes)

    def test_outside_plan_reference_is_rejected_without_state_or_execution(self):
        self.task["plan_ref"] = {"path": "../unrelated.md"}
        with self.assertRaisesRegex(RuntimeError, "inside this worktree"):
            product.start_task(self.c, self.task)
        self.assertEqual(self.f.calls(), [])
        self.assertFalse((self.c.root/"active.json").exists())

    def test_missing_dependency_cannot_start(self):
        self.task["depends_on"] = ["NOT-DONE"]
        with self.assertRaisesRegex(RuntimeError, "no recorded result"):
            product.start_task(self.c, self.task)
        self.assertEqual(self.f.calls(), [])

    def test_saved_pass_is_not_presented_as_current_after_external_edit(self):
        self.call({"action": "start", "task": self.task})
        (self.f.repo/"value.py").write_text("VALUE = -1\n", encoding="utf-8")
        result = self.call({"action": "status"})
        self.assertFalse(result["verified"])
        self.assertFalse(result["snapshot_current"])
        self.assertEqual(len(self.f.calls()), 1)
        self.assertIn("先前快照", result["message"])

    def test_legacy_task_without_contract_file_can_resume(self):
        self.f.create()
        self.c.run(self.f.ticket_id)
        self.c.verdict(self.f.ticket_id, self.f.review("REVISE"), source="test")
        (self.c.ticket_dir(self.f.ticket_id)/"contract.json").unlink()
        result = self.call({"action": "continue"})
        self.assertTrue(result["verified"])
        self.assertIn(str(self.c.ticket_dir(self.f.ticket_id)/"ticket.json"), self.f.calls()[1]["prompt"])


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agy-codex-install-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)/"home with spaces"
        self.home.mkdir()

    def test_global_install_is_a_native_plugin_with_runnable_scripts(self):
        result = installer.install(home=self.home)
        plugin = Path(result["plugin"])
        self.assertEqual(plugin, self.home/".gemini/config/plugins/agy-codex")
        self.assertTrue((plugin/"agents/agy-codex.md").is_file())
        self.assertEqual(json.loads((plugin/"plugin.json").read_text())["name"], "agy-codex")
        self.assertFalse((self.home/".agents").exists())
        self.assertFalse((self.home/".gemini/config/skills/agy-codex").exists())
        self.assertFalse((plugin/"tests").exists())
        command = subprocess.run([sys.executable, str(plugin/"skills/agy-codex/scripts/user_cli.py"), "--help"], capture_output=True, text=True)
        self.assertEqual(command.returncode, 0, command.stderr)

    def test_reinstall_is_idempotent(self):
        installer.install(home=self.home)
        again = installer.install(home=self.home)
        self.assertFalse(again["changed"])
        self.assertIsNone(again["backup"])

    def test_existing_unowned_plugin_is_preserved(self):
        target = self.home/".gemini/config/plugins/agy-codex"
        target.mkdir(parents=True)
        (target/"personal.txt").write_text("Existing user plugin", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "existing directory was preserved"):
            installer.install(home=self.home)
        self.assertEqual((target/"personal.txt").read_text(), "Existing user plugin")

    def test_non_object_ownership_marker_has_readable_error_and_preserves_directory(self):
        target = self.home/".gemini/config/plugins/agy-codex"
        target.mkdir(parents=True)
        (target/"INSTALLATION.json").write_text("[]")
        with self.assertRaisesRegex(ValueError, "existing directory was preserved"):
            installer.install(home=self.home)
        self.assertEqual((target/"INSTALLATION.json").read_text(), "[]")

    def test_edited_owned_install_is_backed_up_outside_discovery(self):
        first = installer.install(home=self.home)
        modified = Path(first["plugin"])/"skills/agy-codex/references/execution.md"
        modified.write_text("My customization", encoding="utf-8")
        result = installer.install(home=self.home)
        self.assertTrue(result["changed"])
        backup = Path(result["backup"])
        self.assertEqual((backup/"skills/agy-codex/references/execution.md").read_text(), "My customization")
        self.assertNotIn(backup, (self.home/".gemini/config/plugins").rglob("*"))
        self.assertEqual(len(list((self.home/".gemini/config/plugins").iterdir())), 1)

    def test_project_install_does_not_add_a_global_copy(self):
        project = self.home/"project"
        project.mkdir()
        result = installer.install(home=self.home, project=project)
        self.assertEqual(result["scope"], "project")
        self.assertTrue((project/".agents/plugins/agy-codex/skills/agy-codex/SKILL.md").is_file())
        self.assertFalse((self.home/".gemini").exists())

    def test_uninstall_preserves_customization_and_task_state(self):
        project = self.home/"project"
        project.mkdir()
        state = project/".git/superagent/tickets/saved.json"
        state.parent.mkdir(parents=True)
        state.write_text("saved session")
        old_skill = project/".agents/skills/personal/SKILL.md"
        old_skill.parent.mkdir(parents=True)
        old_skill.write_text("personal method")
        first = installer.install(home=self.home, project=project)
        result = installer.install(home=self.home, project=project, uninstall=True)
        self.assertFalse(Path(first["plugin"]).exists())
        self.assertTrue((Path(result["backup"])/"plugin.json").exists())
        self.assertEqual(state.read_text(), "saved session")
        self.assertEqual(old_skill.read_text(), "personal method")

    def test_failed_update_restores_previous_install(self):
        first = installer.install(home=self.home)
        target = Path(first["plugin"])
        (target/"agents/agy-codex.md").write_text("local agent customization")
        original_replace = installer.os.replace
        def fail_stage(source, destination):
            if Path(source).name.startswith("stage-") and Path(destination) == target:
                raise OSError("Injected atomic install failure")
            return original_replace(source, destination)
        with mock.patch.object(installer.os, "replace", side_effect=fail_stage):
            with self.assertRaisesRegex(OSError, "Injected"):
                installer.install(home=self.home)
        self.assertEqual((target/"agents/agy-codex.md").read_text(), "local agent customization")

    @unittest.skipIf(sys.platform == "win32", "Symlink fixture needs POSIX; real Windows remains a host check")
    def test_dangling_symlink_destination_is_not_followed(self):
        target = self.home/".gemini/config/plugins/agy-codex"
        target.parent.mkdir(parents=True)
        target.symlink_to(self.home/"missing-user-directory")
        with self.assertRaisesRegex(ValueError, "linked destination"):
            installer.install(home=self.home)
        self.assertTrue(target.is_symlink())
        self.assertFalse((self.home/"missing-user-directory").exists())


if __name__ == "__main__":
    unittest.main()
