"""Offline protocol integration tests; no Codex credentials or model calls.

Every scenario invokes a separate test-double executable against a disposable
Git repository. One failure-injection test overrides the PID-save boundary.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock


SOURCE = Path(os.environ.get("SUPERAGENT_SOURCE", str(Path(__file__).resolve().parents[1] / "skills/agy-codex/scripts/controller.py")))
sa = sys.modules.get("controller")
if sa is None or Path(sa.__file__).resolve() != SOURCE.resolve():
    module_spec = importlib.util.spec_from_file_location("controller", SOURCE)
    sa = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = sa
    module_spec.loader.exec_module(sa)
FAKE = Path(__file__).with_name("fake_codex.py").resolve()
sys.path.insert(0, str(SOURCE.parent))
import product


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agy-codex-protocol-")
        self.addCleanup(self.temp.cleanup)
        self.global_config = Path(self.temp.name) / "agy-config.json"
        patch = mock.patch.dict(os.environ, {"AGY_CODEX_CONFIG": str(self.global_config)})
        patch.start()
        self.addCleanup(patch.stop)
        self.repo = Path(self.temp.name) / "repo with spaces"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "core.hooksPath", str(Path(self.temp.name) / "disabled-hooks"))
        self.git("config", "user.name", "AGY Codex Test Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgSign", "false")
        (self.repo / "value.py").write_text("VALUE = 0\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        self.git("add", "value.py", ".gitignore")
        self.git("commit", "-q", "-m", "Isolated test fixture")
        self.control = self.repo / ".git" / "fake-codex-control.json"
        self.ledger = self.repo / ".git" / "fake-codex-calls.jsonl"
        self.controller = self.new_controller()
        self.ticket_id = "TEST-001"

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.repo), *args],
                                capture_output=True, text=True, check=True,
                                env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
        return result.stdout.strip()

    def new_controller(self):
        return sa.Controller(self.repo, codex_cmd=[sys.executable, str(FAKE)], timeout=10)

    def create(self, **changes):
        spec = {"id": self.ticket_id, "title": "Fix the fixture value",
                "goal": "Set VALUE to a positive integer.",
                "acceptance": ["VALUE is a positive integer"],
                "test_commands": [["{python}", "-c", "from value import VALUE; assert VALUE > 0"]],
                "allowed_paths": ["value.py"], "max_rounds": 3}
        spec.update(changes)
        return self.controller.create(spec)

    def mode(self, name, **extra):
        self.control.write_text(json.dumps(dict(mode=name, **extra)), encoding="utf-8")

    def calls(self):
        return [json.loads(line) for line in self.ledger.read_text().splitlines()] if self.ledger.exists() else []

    def run_with_bound_session_timeout(self, ticket_id=None):
        """Expire only after the real child has emitted a persisted Session ID.

        Startup gets the normal timeout budget, including Windows Job Object
        setup. Only the controller's clock is advanced; process cleanup and
        subsequent resume still execute normally against real test processes.
        """
        self.mode("hold", seconds=30, startup_delay=0.25)
        bound = False
        original_save = self.controller.save

        def save(state, event=None):
            nonlocal bound
            result = original_save(state, event)
            if event == "session_bound":
                bound = True
            return result

        clock = SimpleNamespace(
            monotonic=lambda: time.monotonic() + (self.controller.timeout + 1 if bound else 0),
            strftime=time.strftime, gmtime=time.gmtime,
        )
        with mock.patch.object(sa, "time", clock), mock.patch.object(self.controller, "save", side_effect=save):
            return self.controller.run(ticket_id or self.ticket_id)

    def review(self, decision="PASS", packet=None):
        packet = packet or self.controller.packet(self.ticket_id)
        findings = [] if decision == "PASS" else [{"id": "F-001", "path": "value.py",
                    "reason": "The requested follow-up remains incomplete.",
                    "requested_fix": "Update the fixture again without changing acceptance tests."}]
        return {"ticket_id": self.ticket_id, "round": packet["round"],
                "snapshot_sha256": packet["snapshot_sha256"], "verdict": decision,
                "summary": "Fixture checks reviewed.", "findings": findings,
                "axes": {"spec": "Fixture behavior verified.", "standards": "Fixture conventions reviewed."}, "blocker": None}

    def test_pass_has_independent_tests_diff_and_persistent_session(self):
        created = self.create()
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(created["status"], "READY")
        state = self.controller.run(self.ticket_id)
        self.assertEqual(state["status"], "REVIEW", state)
        packet = self.controller.packet(self.ticket_id)
        self.assertTrue(packet["all_tests_passed"])
        self.assertEqual(packet["changed_files"], ["value.py"])
        self.assertIn("+VALUE = 1", Path(packet["diff_path"]).read_text())
        summary = json.loads(Path(packet["summary_path"]).read_text())
        self.assertTrue(summary["completed"])
        self.assertEqual(summary["session_id"], state["session_id"])
        # Reconstructing the controller simulates a new Router conversation.
        self.controller = self.new_controller()
        result = self.controller.verdict(self.ticket_id, self.review(), source="test")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertFalse(self.calls()[0]["resume"])

    def test_revise_resumes_exact_session_in_new_controller(self):
        self.create()
        first = self.controller.run(self.ticket_id)
        self.controller.verdict(self.ticket_id, self.review("REVISE"), source="test")
        self.controller = self.new_controller()
        second = self.controller.run(self.ticket_id)
        self.assertEqual(second["status"], "REVIEW", second)
        self.assertEqual(second["round"], 2)
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertTrue(self.calls()[1]["resume"])
        self.assertEqual(self.calls()[1]["requested_session"], first["session_id"])
        self.assertIn("F-001", self.calls()[1]["prompt"])

    def test_worker_settings_are_isolated_and_resume_can_upgrade(self):
        sa.atomic_json(self.global_config, {"codex": {"model": "example-global", "reasoning_effort": "low"}})
        sa.atomic_json(self.controller.root / "config.json", {"codex": {"reasoning_effort": "medium"}})
        state = self.create(codex={"model": "example-task"})
        self.assertEqual(state["codex_requested"], {"model": "example-task", "reasoning_effort": "medium"})
        first = self.controller.run(self.ticket_id)
        self.assertEqual(first["status"], "REVIEW", first)
        args = self.calls()[0]["argv"]
        self.assertEqual(args[args.index("-m")+1], "example-task")
        self.assertEqual(args[args.index("-c")+1], 'model_reasoning_effort="medium"')
        self.controller.verdict(self.ticket_id, self.review("REVISE"), source="test")
        sa.atomic_json(self.global_config, {"codex": {"model": "different-default"}})
        self.controller = self.new_controller()
        second = self.controller.run(self.ticket_id, codex={"reasoning_effort": "high"})
        self.assertEqual(second["status"], "REVIEW", second)
        self.assertEqual(second["session_id"], first["session_id"])
        args = self.calls()[1]["argv"]
        self.assertEqual(args[args.index("-m")+1], "example-task")
        self.assertEqual(args[args.index("-c")+1], 'model_reasoning_effort="high"')
        record = sa.read_json(self.controller.ticket_dir(self.ticket_id) / "rounds" / "02" / "invocation.json")
        self.assertEqual(record["codex_requested"], second["codex_requested"])
        self.assertEqual(product.report(self.controller, second)["codex_requested"], second["codex_requested"])

    def test_invalid_model_override_does_not_consume_round(self):
        self.create()
        with self.assertRaises(sa.WorkflowError):
            self.controller.run(self.ticket_id, codex={"reasoning_effort": "high\\nother=true"})
        self.assertEqual(self.controller.load(self.ticket_id)["round"], 0)
        self.assertEqual(self.calls(), [])

    def test_third_revise_escalates_without_fourth_worker(self):
        self.create()
        for expected_round in (1, 2, 3):
            state = self.controller.run(self.ticket_id)
            self.assertEqual(state["round"], expected_round)
            self.assertEqual(state["status"], "REVIEW", state)
            state = self.controller.verdict(self.ticket_id, self.review("REVISE"), source="test")
            self.assertEqual(state["status"], "ESCALATE" if expected_round == 3 else "REVISE")
        with self.assertRaises(sa.WorkflowError):
            self.controller.run(self.ticket_id)
        self.assertEqual(len(self.calls()), 3)

    def test_mismatched_resume_id_escalates_and_preserves_original_mapping(self):
        self.create()
        first = self.controller.run(self.ticket_id)
        self.controller.verdict(self.ticket_id, self.review("REVISE"), source="test")
        self.mode("mismatch-resume")
        result = self.controller.run(self.ticket_id)
        self.assertEqual(result["status"], "ESCALATE")
        self.assertEqual(result["session_id"], first["session_id"])
        self.assertIn("different Session ID", result["reason"])

    def test_zero_exit_without_completed_event_cannot_enter_review(self):
        self.create()
        self.mode("missing-completed")
        result = self.controller.run(self.ticket_id)
        self.assertEqual(result["status"], "ESCALATE")
        summary = json.loads((self.controller.ticket_dir(self.ticket_id) / "rounds/01/summary.json").read_text())
        self.assertEqual(summary["exit_code"], 0)
        self.assertFalse(summary["completed"])

    def test_malformed_jsonl_cannot_enter_review(self):
        self.create()
        self.mode("malformed")
        result = self.controller.run(self.ticket_id)
        self.assertEqual(result["status"], "ESCALATE")
        summary = json.loads((self.controller.ticket_dir(self.ticket_id) / "rounds/01/summary.json").read_text())
        self.assertEqual(summary["invalid_lines"], 1)
        self.assertTrue(summary["completed"])

    def test_resume_requires_returned_session_identity(self):
        self.create()
        first = self.controller.run(self.ticket_id)
        self.controller.verdict(self.ticket_id, self.review("REVISE"), source="test")
        self.mode("missing-thread")
        state = self.controller.run(self.ticket_id)
        self.assertEqual(state["status"], "ESCALATE")
        self.assertEqual(state["session_id"], first["session_id"])

    def test_same_path_edit_invalidates_verdict_and_refresh_retests(self):
        self.create()
        self.controller.run(self.ticket_id)
        old_packet = self.controller.packet(self.ticket_id)
        old_review = self.review(packet=old_packet)
        (self.repo / "value.py").write_text("VALUE = 99\n", encoding="utf-8")
        with self.assertRaises(sa.WorkflowError):
            self.controller.verdict(self.ticket_id, old_review, source="test")
        self.assertEqual(self.controller.load(self.ticket_id)["status"], "REVIEW")
        self.controller.refresh(self.ticket_id)
        new_packet = self.controller.packet(self.ticket_id)
        self.assertNotEqual(new_packet["snapshot_sha256"], old_packet["snapshot_sha256"])
        self.assertEqual(new_packet["round"], old_packet["round"])
        self.assertTrue(new_packet["all_tests_passed"])
        with self.assertRaises(sa.WorkflowError):
            self.controller.verdict(self.ticket_id, old_review, source="test")

    def test_failed_independent_test_prevents_pass(self):
        self.create(test_commands=[["{python}", "-c", "raise SystemExit(7)"]])
        self.controller.run(self.ticket_id)
        packet = self.controller.packet(self.ticket_id)
        self.assertFalse(packet["all_tests_passed"])
        self.assertEqual(packet["tests"][0]["exit_code"], 7)
        with self.assertRaises(sa.WorkflowError):
            self.controller.verdict(self.ticket_id, self.review(), source="test")
        self.assertEqual(self.controller.load(self.ticket_id)["status"], "REVIEW")

    def test_worktree_lock_rejects_separate_process_before_worker_starts(self):
        self.create()
        script = (
            "import importlib.util,sys; "
            "s=importlib.util.spec_from_file_location('sa',sys.argv[1]); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            "c=m.Controller(sys.argv[2],codex_cmd=[sys.executable,sys.argv[3]],timeout=10); "
            "c.run(sys.argv[4])"
        )
        with sa.worktree_lock(self.controller.lock):
            child = subprocess.run([sys.executable, "-c", script, str(SOURCE), str(self.repo),
                                    str(FAKE), self.ticket_id], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(child.returncode, 0)
        self.assertIn("already has a running controller", child.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.controller.load(self.ticket_id)["round"], 0)

    def test_untracked_addition_and_tracked_deletion_are_both_reviewed(self):
        self.create(allowed_paths=["value.py", "created.txt"],
                    test_commands=[["{python}", "-c", "from pathlib import Path; assert Path('created.txt').read_text() == 'new untracked evidence\\n'; assert not Path('value.py').exists()"]])
        self.mode("add-delete")
        state = self.controller.run(self.ticket_id)
        self.assertEqual(state["status"], "REVIEW", state)
        packet = self.controller.packet(self.ticket_id)
        self.assertEqual(packet["changed_files"], ["created.txt", "value.py"])
        diff = Path(packet["diff_path"]).read_text()
        self.assertIn("deleted file mode", diff)
        self.assertIn("new file mode", diff)
        self.assertIn("+new untracked evidence", diff)

    def test_dirty_baseline_is_preserved_and_diff_contains_only_this_task(self):
        dirty = "VALUE = 8765\n"
        (self.repo / "value.py").write_text(dirty, encoding="utf-8")
        (self.repo / "user-notes.txt").write_text("Pre-existing unrelated notes\n", encoding="utf-8")
        self.git("add", "value.py")
        index_before = self.git("ls-files", "--stage")
        self.create()
        self.assertEqual((self.repo / "value.py").read_text(), dirty)
        self.assertEqual(self.calls(), [])
        self.controller.run(self.ticket_id)
        packet = self.controller.packet(self.ticket_id)
        diff = Path(packet["diff_path"]).read_text()
        self.assertIn("-VALUE = 8765", diff)
        self.assertIn("+VALUE = 1", diff)
        self.assertNotIn("-VALUE = 0", diff)
        self.assertNotIn("user-notes", diff)
        self.assertEqual(self.git("ls-files", "--stage"), index_before)
        self.assertEqual((self.repo / "user-notes.txt").read_text(), "Pre-existing unrelated notes\n")
        self.assertEqual(packet["diff_basis"], "task-start")

    def test_scope_violation_escalates(self):
        self.create(allowed_paths=["unrelated.py"])
        result = self.controller.run(self.ticket_id)
        self.assertEqual(result["status"], "ESCALATE")
        self.assertIn("outside Ticket scope", result["reason"])

    def test_in_scope_supports_directories_globs_and_repo_root(self):
        in_scope = sa.Controller._in_scope
        self.assertTrue(in_scope("src/mod/a.py", ["src/"]))
        self.assertTrue(in_scope("src/mod/a.py", ["src"]))
        self.assertFalse(in_scope("src2/a.py", ["src"]))
        self.assertTrue(in_scope("tests/test_app.py", ["tests/test_*.py"]))
        self.assertFalse(in_scope("tests/helper.py", ["tests/test_*.py"]))
        self.assertTrue(in_scope("pkg/sub/file.ts", ["**/*.ts"]))
        self.assertTrue(in_scope("anything/here.txt", ["."]))
        self.assertTrue(in_scope("src/a.py", [".\\src\\"]))

    def test_globs_are_root_anchored_and_match_complete_path_components(self):
        cases = [
            ("index.ts", "**/*.ts", True),
            ("src/a.py", "src/**/*.py", True),
            ("src/sub/deep/a.py", "src/**/*.py", True),
            ("other/src/sub/a.py", "src/**/*.py", False),
            ("src/sub/a.py", "src/*.py", False),
            ("src/a.py", "*.py", False),
            ("src/a.py", "src/?.py", True),
            ("src/ab.py", "src/?.py", False),
            ("src/a.py", "src/[ab].py", True),
            ("src/c.py", "src/[ab].py", False),
            ("src/sub/a.py", "src/*/", True),
            ("src/sub/deep/a.py", "src/*/", True),
            ("src/a.py", "src/*/", False),
            ("src/sub/a.py", ".\\src\\**\\*.py", True),
        ]
        for name, pattern, expected in cases:
            with self.subTest(name=name, pattern=pattern):
                self.assertEqual(sa.Controller._in_scope(name, [pattern]), expected)

    def test_recursive_glob_allows_root_file_through_review(self):
        self.create(allowed_paths=["**/*.py"])
        state = self.controller.run(self.ticket_id)
        self.assertEqual(state["status"], "REVIEW", state)
        self.assertEqual(self.controller.packet(self.ticket_id)["changed_files"], ["value.py"])
        self.assertEqual(self.controller.verdict(self.ticket_id, self.review())["status"], "PASS")

    def test_explicit_ignored_file_has_baseline_diff_and_stale_review_protection(self):
        (self.repo / ".gitignore").write_text("__pycache__/\nlocal-config.json\ncache/\n")
        config = self.repo / "local-config.json"
        config.write_text("old\n")
        (self.repo / "cache").mkdir()
        (self.repo / "cache" / "unrelated.bin").write_bytes(b"ignored build output")
        self.create(allowed_paths=["local-config.json", "cache/"], test_commands=[
            ["{python}", "-c", "from pathlib import Path; assert Path('local-config.json').read_text() == 'new\\n'"]])
        self.mode("files", files={"local-config.json": "new\n"})
        state = self.controller.run(self.ticket_id)
        self.assertEqual(state["status"], "REVIEW", state)
        packet = self.controller.packet(self.ticket_id)
        self.assertEqual(packet["changed_files"], ["local-config.json"])
        patch = Path(packet["diff_path"]).read_text()
        self.assertIn("-old", patch)
        self.assertIn("+new", patch)
        baseline = sa.read_json(self.controller.ticket_dir(self.ticket_id) / "baseline.json")
        self.assertIn("local-config.json", baseline["files"])
        self.assertNotIn("cache/unrelated.bin", baseline["files"])
        stale = self.review(packet=packet)
        config.write_text("external edit\n")
        self.controller = self.new_controller()
        with self.assertRaisesRegex(sa.WorkflowError, "stale"):
            self.controller.verdict(self.ticket_id, stale)
        config.write_text("new\n")
        state = self.controller.verdict(self.ticket_id, stale)
        self.assertTrue(product.report(self.controller, state)["verified"])
        config.write_text("external edit after PASS\n")
        self.assertFalse(product.report(self.controller, state)["verified"])

    def test_new_explicit_ignored_file_is_captured_without_scanning_ignored_directory(self):
        (self.repo / ".gitignore").write_text("__pycache__/\ncache/\n")
        created = self.create(allowed_paths=["cache/new-config.json"], test_commands=[
            ["{python}", "-c", "from pathlib import Path; assert Path('cache/new-config.json').read_text() == 'new\\n'"]])
        self.assertIn("cache/new-config.json", created["snapshot_paths"])
        self.mode("files", files={"cache/new-config.json": "new\n"})
        state = self.controller.run(self.ticket_id)
        self.assertEqual(state["status"], "REVIEW", state)
        packet = self.controller.packet(self.ticket_id)
        self.assertEqual(packet["changed_files"], ["cache/new-config.json"])
        self.assertIn("new file mode", Path(packet["diff_path"]).read_text())
        self.assertEqual(self.controller.verdict(self.ticket_id, self.review())["status"], "PASS")

    def test_explicit_ignored_file_deletion_and_recreation_invalidate_evidence(self):
        (self.repo / ".gitignore").write_text("__pycache__/\nlocal-config.json\n")
        config = self.repo / "local-config.json"
        config.write_text("old\n")
        self.create(allowed_paths=["local-config.json"], test_commands=[
            ["{python}", "-c", "from pathlib import Path; p=Path('local-config.json'); assert not p.exists() or p.read_text() == 'new\\n'"]])
        self.mode("files", files={"local-config.json": None})
        state = self.controller.run(self.ticket_id)
        self.assertEqual(state["status"], "REVIEW", state)
        packet = self.controller.packet(self.ticket_id)
        self.assertEqual(packet["changed_files"], ["local-config.json"])
        self.assertIn("deleted file mode", Path(packet["diff_path"]).read_text())
        stale = self.review(packet=packet)
        config.write_text("new\n")
        with self.assertRaisesRegex(sa.WorkflowError, "stale"):
            self.controller.verdict(self.ticket_id, stale)
        self.controller.refresh(self.ticket_id)
        refreshed = self.controller.packet(self.ticket_id)
        self.assertNotEqual(packet["snapshot_sha256"], refreshed["snapshot_sha256"])
        self.assertIn("+new", Path(refreshed["diff_path"]).read_text())
        self.assertEqual(self.controller.verdict(self.ticket_id, self.review())["status"], "PASS")

    def test_ignored_file_task_can_transition_to_an_ordinary_task(self):
        (self.repo / ".gitignore").write_text("__pycache__/\nlocal-config.json\n")
        self.create(allowed_paths=["local-config.json"], test_commands=[
            ["{python}", "-c", "from pathlib import Path; assert Path('local-config.json').read_text() == 'new\\n'"]])
        self.mode("files", files={"local-config.json": "new\n"})
        self.controller.run(self.ticket_id)
        first = self.controller.verdict(self.ticket_id, self.review())
        self.ticket_id = "NEXT"
        self.controller.create({"id": self.ticket_id, "title": "Follow-up", "goal": "Update VALUE",
                                "acceptance": ["VALUE is positive"], "allowed_paths": ["value.py"],
                                "test_commands": [["{python}", "-c", "from value import VALUE; assert VALUE > 0"]]},
                               expected_sha256=first["expected_sha256"])
        self.mode("success")
        state = self.controller.run(self.ticket_id)
        self.assertEqual(state["status"], "REVIEW", state)
        state = self.controller.verdict(self.ticket_id, self.review())
        self.controller = self.new_controller()
        self.assertTrue(product.report(self.controller, state)["verified"])
        self.assertEqual(self.controller.snapshot()["sha256"], state["expected_sha256"])
        (self.repo / "local-config.json").write_text("regression\n")
        self.assertFalse(product.report(self.controller, state)["verified"])

    def test_legacy_tickets_keep_visible_file_evidence_but_reject_ignored_targets(self):
        state = self.create()
        del state["snapshot_paths"]
        self.controller.save(state)
        self.assertEqual(self.controller.run(self.ticket_id)["status"], "REVIEW")
        self.controller.verdict(self.ticket_id, self.review())
        self.ticket_id = "LEGACY-IGNORED"
        (self.repo / ".gitignore").write_text("__pycache__/\nlocal-config.json\n")
        state = self.create(allowed_paths=["local-config.json"])
        del state["snapshot_paths"]
        self.controller.save(state)
        with self.assertRaisesRegex(sa.WorkflowError, "legacy Ticket.*ignored files"):
            self.controller.run(self.ticket_id)
        self.assertEqual(len(self.calls()), 1)

    def test_ticket_round_limit_cannot_exceed_three(self):
        with self.assertRaises(sa.WorkflowError):
            self.create(max_rounds=4)
        self.assertEqual(self.calls(), [])

    def test_timeout_bound_session_can_recover_and_resume(self):
        self.create()
        interrupted = self.run_with_bound_session_timeout()
        self.assertEqual(interrupted["status"], "ESCALATE", interrupted)
        self.assertIn("Process timeout", interrupted["reason"])
        self.assertTrue(interrupted.get("recoverable_interruption"), interrupted)
        self.assertTrue(interrupted["session_id"])
        self.assertIsNone(interrupted["child_pid"])
        self.assertFalse(sa.pid_alive(interrupted["last_child_pid"]))
        # The replacement Router has no in-memory state from the timed-out run.
        self.controller = self.new_controller()
        recovered = self.controller.recover(self.ticket_id)
        self.assertEqual(recovered["status"], "REVISE", recovered)
        self.assertEqual(recovered["round"], 1)
        self.mode("success")
        resumed = self.controller.run(self.ticket_id)
        self.assertEqual(resumed["status"], "REVIEW", resumed)
        self.assertEqual(resumed["round"], 2)
        self.assertEqual(resumed["session_id"], interrupted["session_id"])
        self.assertEqual(self.calls()[1]["requested_session"], interrupted["session_id"])
        self.assertTrue(self.calls()[1]["resume"])
        result = self.controller.verdict(self.ticket_id, self.review(), source="test")
        self.assertEqual(result["status"], "PASS")

    @unittest.skipIf(os.name == "nt", "POSIX orphan process-group regression; Windows uses Job Objects")
    def test_descendant_cannot_write_after_worker_leader_exits(self):
        self.create()
        self.mode("orphan-descendant")
        state = self.controller.run(self.ticket_id)
        pid_path = self.repo / ".git" / "fake-descendant-pid"
        self.assertTrue(pid_path.exists(), "The child fixture must actually have been spawned")
        self.assertTrue((self.repo / ".git" / "fake-descendant-ready").exists())
        descendant_pid = int(pid_path.read_text())
        def cleanup():
            import signal
            try:
                os.kill(descendant_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.addCleanup(cleanup)
        self.assertEqual(state["status"], "REVIEW", state)
        # This child ignores SIGTERM; it must not outlive its already exited leader.
        time.sleep(0.85)
        self.assertFalse((self.repo / ".git" / "fake-descendant-wrote").exists())
        self.assertEqual((self.repo / "value.py").read_text(), "VALUE = 1\n")
        self.assertEqual(self.controller.packet(self.ticket_id)["round"], 1)

    def test_failure_to_persist_started_pid_still_cleans_worker(self):
        class FailingStartSave(sa.Controller):
            observed_pid = None
            def save(inner, state, event=None):
                if event == "process_started":
                    inner.observed_pid = state["child_pid"]
                    raise OSError("Injected failure saving process_started")
                return super().save(state, event)
        self.controller = FailingStartSave(self.repo, codex_cmd=[sys.executable, str(FAKE)], timeout=10)
        self.create()
        result = self.controller.run(self.ticket_id)
        self.assertEqual(result["status"], "ESCALATE", result)
        self.assertIn("Injected failure", result["reason"])
        self.assertIsInstance(self.controller.observed_pid, int)
        self.assertFalse(sa.pid_alive(self.controller.observed_pid))
        self.assertIsNone(result["child_pid"])
        self.assertEqual((self.repo / "value.py").read_text(), "VALUE = 0\n")

    def test_empty_round_reservation_is_recoverable(self):
        self.create()
        (self.controller.ticket_dir(self.ticket_id)/"rounds/01").mkdir(parents=True)
        state = self.controller.run(self.ticket_id)
        self.assertEqual(state["status"], "REVIEW", state)
        self.assertEqual(state["round"], 1)
        self.assertEqual(len(self.calls()), 1)

    def test_completed_worker_recovery_does_not_invoke_worker_again(self):
        self.create()
        state = self.controller.run(self.ticket_id)
        # Simulate a crash after packet persistence but before REVIEW publication.
        state.update(status="RUNNING", runner_pid=None, child_pid=None)
        self.controller.save(state)
        recovered = self.new_controller().recover(self.ticket_id)
        self.assertEqual(recovered["status"], "REVIEW", recovered)
        self.assertEqual(recovered["round"], 1)
        self.assertEqual(len(self.calls()), 1)

    def test_prompt_metacharacters_remain_stdin_data(self):
        text = '保持中文和换行\nquote " backtick ` dollar $(echo INJECTION) & semicolon ;'
        self.create(goal=text)
        # The wire protocol stays UTF-8 even when the child uses another locale.
        with mock.patch.dict(os.environ, {"PYTHONIOENCODING": "cp1252"}):
            state = self.controller.run(self.ticket_id)
        self.assertEqual(state["status"], "REVIEW", state)
        call = self.calls()[0]
        self.assertEqual(call["argv"][-1], "-")
        self.assertNotIn(text, call["argv"])
        self.assertIn('$(echo INJECTION)', call["prompt"])
        self.assertIn('保持中文和换行', call["prompt"])

    def test_launch_gate_waits_and_preserves_prompt_bytes(self):
        marker = Path(self.temp.name)/"gate-started"
        code = "import sys; from pathlib import Path; Path(sys.argv[1]).write_text('started'); sys.stdout.buffer.write(sys.stdin.buffer.read())"
        argv = [sys.executable, "-c", code, str(marker)]
        p = subprocess.Popen([sys.executable, str(SOURCE), "_child-gate", json.dumps(argv)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(0.1)
            self.assertFalse(marker.exists(), "The real child must not start before release")
            payload = ('Unicode 中文, quotes " ` $() &\n' * 300).encode('utf-8')
            out, err = p.communicate(b"1"+payload, timeout=5)
            self.assertEqual(p.returncode, 0, err)
            self.assertEqual(out, payload)
            self.assertTrue(marker.exists())
        finally:
            if p.poll() is None:
                p.kill(); p.communicate(timeout=5)

    @unittest.skipIf(os.name == "nt", "POSIX crash window; Windows Job Objects terminate the owned job on parent exit")
    def test_recovery_refuses_surviving_worker_after_parent_crash(self):
        self.create()
        self.mode("hold", seconds=5)
        script = (
            "import importlib.util,sys; "
            "s=importlib.util.spec_from_file_location('sa',sys.argv[1]); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            "m.Controller(sys.argv[2],codex_cmd=[sys.executable,sys.argv[3]],timeout=10).run(sys.argv[4])"
        )
        parent = subprocess.Popen([sys.executable, "-c", script, str(SOURCE), str(self.repo), str(FAKE), self.ticket_id],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child_pid = None
        try:
            deadline = time.monotonic()+3
            while time.monotonic() < deadline:
                saved = self.controller.load(self.ticket_id)
                if saved.get("child_pid") and saved.get("session_id"):
                    child_pid = saved["child_pid"]
                    break
                time.sleep(0.01)
            self.assertIsNotNone(child_pid)
            parent.kill(); parent.wait(timeout=5)
            with self.assertRaisesRegex(sa.WorkflowError, "still alive"):
                self.new_controller().recover(self.ticket_id)
            self.assertEqual(len(self.calls()), 1)
        finally:
            import signal
            if parent.poll() is None:
                parent.kill(); parent.wait(timeout=5)
            if child_pid:
                try:
                    os.killpg(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    unittest.main()
