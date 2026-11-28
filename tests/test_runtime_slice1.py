"""Targeted Slice 1 regression tests for shared runtime and lightweight CLI."""
from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/agy-codex/scripts"
sys.path.insert(0, str(SCRIPTS))
import controller as sa
import inbox
import runtime
import user_cli

FAKE_APP = Path(__file__).with_name("fake_app_server.py").resolve()
FAKE_EXEC = Path(__file__).with_name("fake_codex.py").resolve()


class Slice1RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agy-s1-")).resolve()
        self.addCleanup(lambda: sa.shutil.rmtree(self.tmp, ignore_errors=True))
        self.state_root = self.tmp / "state-root"
        self.mutex_root = self.tmp / "mutex-root"
        self.global_cfg = self.tmp / "global-config.json"
        self.fake_ctrl_dir = self.tmp / "fake-ctrl"
        self.fake_ctrl_dir.mkdir(parents=True, exist_ok=True)
        self.codex_home = self.tmp / "codex-home"
        self.codex_home.mkdir(parents=True, exist_ok=True)
        env = mock.patch.dict(os.environ, {
            "AGY_CODEX_STATE_DIR": str(self.state_root),
            "AGY_CODEX_CONFIG": str(self.global_cfg),
            "AGY_FAKE_CONTROL_DIR": str(self.fake_ctrl_dir),
            "CODEX_HOME": str(self.codex_home),
        })
        env.start()
        self.addCleanup(env.stop)
        mp = mock.patch.object(runtime, "_mutex_base", return_value=self.mutex_root)
        mp.start()
        self.addCleanup(mp.stop)
        self.configure(backend="app-server")

    def configure(self, **data):
        runtime.atomic_json(self.global_cfg, data)

    def mode(self, **data):
        runtime.atomic_json(self.fake_ctrl_dir / "fake-app-control.json", data)

    def calls(self, method=None):
        p = self.fake_ctrl_dir / "fake-app-calls.jsonl"
        if not p.exists():
            return []
        rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [r for r in rows if r.get("method") == method] if method else rows

    def make_nongit(self, name="项目 文档 A"):
        d = self.tmp / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "source.txt").write_text("hello source\n", encoding="utf-8")
        return d

    def make_git_repo(self, name="repo"):
        d = self.tmp / name
        d.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", str(d)], check=True, capture_output=True)
        (d / "value.py").write_text("VALUE = 0\n", encoding="utf-8")
        hooks = d / ".git" / "empty-hooks"
        hooks.mkdir(exist_ok=True)
        subprocess.run(["git", "-C", str(d), "add", "--", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(d), "-c", "user.name=Test", "-c", "user.email=test@localhost",
             "-c", "core.hooksPath=" + str(hooks), "-c", "commit.gpgsign=false",
             "commit", "-m", "init"],
            check=True, capture_output=True,
        )
        return d

    def test_nongit_unicode_path_run_resume_and_token_filtering(self):
        doc_dir = self.make_nongit("文档 项目 01")
        ws = runtime.resolve_workspace(doc_dir)
        self.assertFalse(ws.is_git)
        self.mode(replay_previous_usage=True)

        first, sum1 = runtime.execute_run(
            ws, "Extract requirements once.",
            codex={"model": "fixture-model", "reasoning_effort": "low"},
            codex_cmd=[sys.executable, str(FAKE_APP)],
            timeout=15,
        )
        self.assertEqual(first["status"], "completed", first)
        self.assertTrue(first["thread_id"])
        self.assertEqual(first["usage"], {"input_tokens": 12, "cached_input_tokens": 2, "output_tokens": 8})
        # No hidden metadata inside the user's non-Git deliverable directory.
        self.assertEqual(sorted(p.name for p in doc_dir.iterdir()), ["source.txt", "value.py"])
        # No hot-path model/list discovery call on normal run!
        self.assertEqual(self.calls("model/list"), [])
        # Optional outputSchema omitted for lightweight run.
        self.assertNotIn("outputSchema", self.calls("turn/start")[0]["params"])

        second, _ = runtime.execute_run(
            ws, "Generate second deliverable using extracted facts.",
            resume="last",
            codex={"model": "fixture-upgrade", "reasoning_effort": "high"},
            codex_cmd=[sys.executable, str(FAKE_APP)],
            timeout=15,
        )
        self.assertEqual(second["status"], "completed", second)
        self.assertEqual(second["thread_id"], first["thread_id"])
        self.assertEqual(second["continued_from"], first["run_id"])
        # Replayed prior turn usage must be filtered; only second turn delta counts.
        self.assertEqual(second["usage"], {"input_tokens": 12, "cached_input_tokens": 2, "output_tokens": 8})
        resume_call = self.calls("thread/resume")[0]["params"]
        self.assertEqual(resume_call["threadId"], first["thread_id"])
        self.assertTrue(resume_call.get("excludeTurns"))

    def test_read_only_resume_enforces_read_only_sandbox_policy(self):
        doc_dir = self.make_nongit("ro-proj")
        ws = runtime.resolve_workspace(doc_dir)
        first, _ = runtime.execute_run(
            ws, "Initial write.",
            codex_cmd=[sys.executable, str(FAKE_APP)], timeout=15,
        )
        self.assertEqual(first["status"], "completed")
        second, _ = runtime.execute_run(
            ws, "Read-only inspection.",
            resume=first["run_id"],
            sandbox="read-only",
            codex_cmd=[sys.executable, str(FAKE_APP)], timeout=15,
        )
        self.assertEqual(second["status"], "completed")
        self.assertEqual(self.calls("thread/resume")[0]["params"]["sandbox"], "read-only")
        self.assertEqual(self.calls("turn/start")[1]["params"]["sandboxPolicy"]["type"], "readOnly")

    def test_nongit_exec_fallback_passes_skip_git_repo_check(self):
        self.configure(backend="auto")
        self.mode(init_error=True)
        doc_dir = self.make_nongit("exec-fallback-dir")
        ws = runtime.resolve_workspace(doc_dir)
        state, _ = runtime.execute_run(
            ws, "Do non-git work via exec fallback.",
            codex_cmd=[sys.executable, str(FAKE_APP)], timeout=15,
        )
        self.assertEqual(state["status"], "completed", state)
        self.assertEqual(state["backend"], "exec")
        inv = runtime.read_json(Path(state["artifact_dir"]) / "invocation.json")
        self.assertIn("--skip-git-repo-check", inv["argv"])
        self.assertNotIn("--output-schema", inv["argv"])

    def test_parent_child_overlap_blocked_but_sibling_prefix_allowed(self):
        parent = self.make_nongit("Project")
        child = parent / "sub"
        child.mkdir()
        sibling = self.make_nongit("Project2")

        ws_parent = runtime.resolve_workspace(parent)
        ws_child = runtime.resolve_workspace(child)
        ws_sibling = runtime.resolve_workspace(sibling)

        with runtime.acquire_writer_lease(ws_parent, "RUN-PARENT"):
            with self.assertRaisesRegex(runtime.WorkflowError, "Overlapping workspace"):
                with runtime.acquire_writer_lease(ws_child, "RUN-CHILD"):
                    pass
            # Sibling with shared string prefix ("Project" vs "Project2") must NOT collide!
            with runtime.acquire_writer_lease(ws_sibling, "RUN-SIBLING"):
                pass
            # Read-only on child is allowed while parent writes.
            with runtime.acquire_writer_lease(ws_child, "RUN-CHILD-RO", sandbox="read-only"):
                pass

    def test_lightweight_write_blocked_by_active_strict_ticket_but_readonly_allowed(self):
        repo = self.make_git_repo("strict-guard")
        c = sa.Controller(repo, codex_cmd=[sys.executable, str(FAKE_APP)], timeout=15)
        c.create({
            "id": "STRICT-1", "title": "Active strict task", "goal": "Keep worktree reserved.",
            "acceptance": ["Tests pass"], "allowed_paths": ["value.py"],
            "test_commands": [["{python}", "-c", "print('ok')"]],
        })
        ws = runtime.resolve_workspace(repo)
        with self.assertRaisesRegex(runtime.WorkflowError, "Strict Ticket"):
            runtime.execute_run(ws, "Try bypassing strict.", codex_cmd=[sys.executable, str(FAKE_APP)], timeout=15)
        # Read-only run is still permitted.
        ro, _ = runtime.execute_run(ws, "Inspect only.", sandbox="read-only",
                                    codex_cmd=[sys.executable, str(FAKE_APP)], timeout=15)
        self.assertEqual(ro["status"], "completed")

    def test_live_inbox_steer_and_ambiguous_reconcile_on_lightweight_run(self):
        doc_dir = self.make_nongit("inbox-live")
        ws = runtime.resolve_workspace(doc_dir)
        self.mode(hold=True)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.addCleanup(pool.shutdown, wait=True)
        cancel = threading.Event()
        self.addCleanup(cancel.set)

        fut = pool.submit(
            runtime.execute_run, ws, "Start live task.",
            codex_cmd=[sys.executable, str(FAKE_APP)], timeout=15, cancel=cancel,
        )
        deadline = time.monotonic() + 10
        active = None
        while time.monotonic() < deadline:
            r = runtime.latest_run(ws)
            if r and r.get("inbox_window") and r.get("active_turn_id"):
                active = r
                break
            time.sleep(0.02)
        self.assertIsNotNone(active, "Lightweight run did not open live inbox window")
        rc = user_cli.main(["--cwd", str(doc_dir), "inbox", "steer", "Adjust output style"])
        self.assertEqual(rc, 0)
        state, _ = fut.result(timeout=15)
        self.assertEqual(state["status"], "completed", state)
        t = inbox.InboxTarget(Path(state["inbox_dir"]), state["run_id"],
                              lambda: runtime.load_run(ws, state["run_id"]), lambda s, e=None: None)
        msgs = inbox.history(t)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["status"], "turn_completed")
        self.assertEqual(msgs[0]["delivery_action"], "steer")

        # Now test ambiguous turn -> status=unknown -> cannot skip on --resume last -> reconcile fixes it.
        self.mode(ambiguous=True, not_loaded=True)
        amb, _ = runtime.execute_run(
            ws, "Ambiguous follow-up.", resume="last",
            codex_cmd=[sys.executable, str(FAKE_APP)], timeout=15,
        )
        self.assertEqual(amb["status"], "unknown", amb)
        self.assertTrue(amb["turn_pending"])
        with self.assertRaisesRegex(runtime.WorkflowError, "Cannot skip an unreconciled Run"):
            runtime.resolve_resume(ws, "last")
        reconciled = runtime.reconcile_run(ws, amb["run_id"], codex_cmd=[sys.executable, str(FAKE_APP)])
        self.assertEqual(reconciled["status"], "completed", reconciled)
        self.assertFalse(reconciled["turn_pending"])

    def test_provider_allowlist_enforced_without_hotpath_discover(self):
        self.configure(backend="app-server", routing={"allowed_providers": ["openai"]})
        doc_dir = self.make_nongit("provider-check")
        ws = runtime.resolve_workspace(doc_dir)
        # Explicit disallowed provider fails before spawning Codex.
        with self.assertRaisesRegex(runtime.WorkflowError, "allowlist"):
            runtime.execute_run(
                ws, "Test provider.", codex={"model_provider": "proxy"},
                codex_cmd=[sys.executable, str(FAKE_APP)], timeout=15,
            )
        self.assertEqual(self.calls(), [])
        # Server-returned disallowed provider aborts before turn/start.
        self.mode(provider="proxy")
        state, _ = runtime.execute_run(
            ws, "Test server provider.", codex_cmd=[sys.executable, str(FAKE_APP)], timeout=15,
        )
        self.assertEqual(state["status"], "failed")
        self.assertEqual(self.calls("turn/start"), [])

    def test_cli_run_status_models_and_nongit_doctor(self):
        doc_dir = self.make_nongit("cli-e2e")
        with mock.patch.object(user_cli, "resolve_codex", return_value=[sys.executable, str(FAKE_APP)]), \
                mock.patch.object(runtime, "resolve_codex", return_value=[sys.executable, str(FAKE_APP)]):
            rc = user_cli.main([
                "--cwd", str(doc_dir),
                "run", "--quiet", "--json", "Quick lightweight edit.",
            ])
            self.assertEqual(rc, 0)
            rc_resume = user_cli.main([
                "--cwd", str(doc_dir),
                "run", "--quiet", "--json", "--resume", "last", "Follow-up edit.",
            ])
            self.assertEqual(rc_resume, 0)
            rc_models = user_cli.main(["--cwd", str(doc_dir), "models", "--json"])
            self.assertEqual(rc_models, 0)

        rc_status = user_cli.main(["--cwd", str(doc_dir), "status", "--json"])
        self.assertEqual(rc_status, 0)
        runs = runtime.list_runs(runtime.resolve_workspace(doc_dir))
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["continued_from"], runs[1]["run_id"])
        self.assertEqual(runs[0]["thread_id"], runs[1]["thread_id"])

        # Non-Git doctor passes without --require-git (mocking codex CLI help probes).
        real_run = subprocess.run
        def fake_subproc(argv, *args, **kwargs):
            if argv[0] == "codex-double":
                return subprocess.CompletedProcess(argv, 0, "ChatGPT --json --output-last-message --output-schema\n", "")
            return real_run(argv, *args, **kwargs)
        with mock.patch.object(sa.subprocess, "run", side_effect=fake_subproc), \
                mock.patch.object(user_cli, "resolve_codex", return_value=["codex-double"]), \
                mock.patch.object(sa, "resolve_codex", return_value=["codex-double"]):
            rc_doc = user_cli.main(["--cwd", str(doc_dir), "doctor", "--json"])
            self.assertEqual(rc_doc, 0)

    def test_external_thread_rejects_cross_workspace_and_exec_unverified(self):
        dir_a = self.make_nongit("ws-a")
        dir_b = self.make_nongit("ws-b")
        ws_a = runtime.resolve_workspace(dir_a)
        ws_b = runtime.resolve_workspace(dir_b)

        first, _ = runtime.execute_run(ws_a, "Start in ws-a.", codex_cmd=[sys.executable, str(FAKE_APP)], timeout=15)
        self.assertEqual(first["status"], "completed")
        ext_tid = first["thread_id"]

        # Resuming ws-a's Thread from ws-b must fail at thread/read cwd check before thread/resume or turn/start.
        before_resumes = len(self.calls("thread/resume"))
        before_turns = len(self.calls("turn/start"))
        bad, _ = runtime.execute_run(
            ws_b, "Cross-workspace resume attempt.", resume=ext_tid,
            codex_cmd=[sys.executable, str(FAKE_APP)], timeout=15,
        )
        self.assertEqual(bad["status"], "failed")
        self.assertIn("different workspace", bad["error"])
        self.assertEqual(len(self.calls("thread/resume")), before_resumes)
        self.assertEqual(len(self.calls("turn/start")), before_turns)

        # Unregistered external Thread on exec backend must be rejected upfront.
        with self.assertRaisesRegex(runtime.WorkflowError, "external Thread ID via exec"):
            runtime.execute_run(
                ws_b, "Exec external resume.", resume=ext_tid, backend_override="exec",
                codex_cmd=[sys.executable, str(FAKE_APP)], timeout=15,
            )

        # External Thread belonging to the same Git worktree (created in a subdir) is accepted.
        repo = self.make_git_repo("git-ext")
        sub = repo / "pkg"
        sub.mkdir()
        fake_thread = {"id": "ext-git-thread-1", "cwd": str(sub), "provider": "openai", "turns": []}
        runtime.atomic_json(self.fake_ctrl_dir / "fake-app-thread.json", fake_thread)
        ws_git = runtime.resolve_workspace(repo)
        ok_run, _ = runtime.execute_run(
            ws_git, "Resume valid external git thread.", resume="ext-git-thread-1",
            codex_cmd=[sys.executable, str(FAKE_APP)], timeout=15,
        )
        self.assertEqual(ok_run["status"], "completed", ok_run)
        self.assertEqual(ok_run["thread_id"], "ext-git-thread-1")

    def test_reconcile_verifies_codex_home_and_paginates_turns_list(self):
        doc_dir = self.make_nongit("paginate-reconcile")
        ws = runtime.resolve_workspace(doc_dir)
        self.mode(ambiguous=True, not_loaded=True)
        amb, _ = runtime.execute_run(
            ws, "Ambiguous run.", codex_cmd=[sys.executable, str(FAKE_APP)], timeout=15,
        )
        self.assertEqual(amb["status"], "unknown")
        self.assertTrue(amb["turn_pending"])

        # Reconcile under a different CODEX_HOME must be rejected.
        other_home = self.tmp / "other-codex-home"
        other_home.mkdir()
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(other_home)}):
            with self.assertRaisesRegex(runtime.WorkflowError, "original CODEX_HOME"):
                runtime.reconcile_run(ws, amb["run_id"], codex_cmd=[sys.executable, str(FAKE_APP)])

        # Insert an unrelated filler turn ahead of active_turn_id and force page_size=1 so target is on page 2.
        tpath = self.fake_ctrl_dir / "fake-app-thread.json"
        tdata = runtime.read_json(tpath)
        tdata["turns"].insert(0, {"id": "filler-turn-page-1", "status": "completed", "error": None, "items": []})
        runtime.atomic_json(tpath, tdata)
        self.mode(not_loaded=True, turns_page_size=1)

        before_list_calls = len(self.calls("thread/turns/list"))
        reconciled = runtime.reconcile_run(ws, amb["run_id"], codex_cmd=[sys.executable, str(FAKE_APP)])
        self.assertEqual(reconciled["status"], "completed", reconciled)
        self.assertFalse(reconciled["turn_pending"])
        # Must have fetched at least 2 pages using nextCursor.
        list_calls = self.calls("thread/turns/list")[before_list_calls:]
        self.assertGreaterEqual(len(list_calls), 2)
        self.assertEqual(list_calls[1]["params"].get("cursor"), "1")

    def test_distinct_state_dirs_cannot_bypass_mutex_and_strict_validation_holds_overlap_lease(self):
        doc_dir = self.make_nongit("shared-mutex-dir")
        state_a = self.tmp / "state-A"
        state_b = self.tmp / "state-B"
        with mock.patch.dict(os.environ, {"AGY_CODEX_STATE_DIR": str(state_a)}):
            ws_a = runtime.resolve_workspace(doc_dir)
        with mock.patch.dict(os.environ, {"AGY_CODEX_STATE_DIR": str(state_b)}):
            ws_b = runtime.resolve_workspace(doc_dir)
        self.assertNotEqual(ws_a.state_dir, ws_b.state_dir)
        self.assertEqual(ws_a.lock_path, ws_b.lock_path)
        with runtime.acquire_writer_lease(ws_a, "RUN-STATE-A"):
            with self.assertRaisesRegex(runtime.WorkflowError, "Overlapping workspace"):
                with runtime.acquire_writer_lease(ws_b, "RUN-STATE-B"):
                    pass

        # Strict validation window must hold overlap lease against a non-Git parent directory writer.
        parent = self.make_nongit("parent-of-git")
        repo = self.make_git_repo("parent-of-git/nested-repo")
        ready = repo / ".git" / "val-ready"
        release = repo / ".git" / "val-release"
        wait_cmd = (
            "from pathlib import Path; import time; "
            f"Path({str(ready)!r}).write_text('1'); "
            "dl = time.monotonic() + 15\n"
            f"while not Path({str(release)!r}).exists() and time.monotonic() < dl: time.sleep(0.02)\n"
            f"assert Path({str(release)!r}).exists()"
        )
        c = sa.Controller(repo, codex_cmd=[sys.executable, str(FAKE_APP)], timeout=20)
        c.create({
            "id": "STRICT-VAL", "title": "Hold validation", "goal": "Verify lease during validation.",
            "acceptance": ["Validation blocks overlapping parent write"], "allowed_paths": ["value.py"],
            "test_commands": [["{python}", "-c", wait_cmd]],
        })
        ws_parent = runtime.resolve_workspace(parent)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.addCleanup(pool.shutdown, wait=True)
        fut = pool.submit(c.run, "STRICT-VAL")
        try:
            dl = time.monotonic() + 12
            while not ready.exists() and time.monotonic() < dl:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "Strict validation did not start")
            with self.assertRaisesRegex(runtime.WorkflowError, "Overlapping workspace"):
                with runtime.acquire_writer_lease(ws_parent, "RUN-PARENT-RACE"):
                    pass
        finally:
            release.write_text("go", encoding="utf-8")
        st = fut.result(timeout=20)
        self.assertEqual(st["status"], "REVIEW", st)
        # Once Strict run finishes and enters REVIEW, parent directory lease is free again.
        with runtime.acquire_writer_lease(ws_parent, "RUN-PARENT-AFTER"):
            pass

    def test_unpublished_strict_plan_manifest_blocks_lightweight_write(self):
        repo = self.make_git_repo("unpublished-plan-repo")
        c = sa.Controller(repo, codex_cmd=[sys.executable, str(FAKE_APP)], timeout=15)
        # Write a non-stopped plan manifest WITHOUT active-plan.json pointer.
        runtime.atomic_json(c.root / "plans" / "PLAN-UNPUB" / "plan.json", {
            "id": "PLAN-UNPUB", "title": "Unpublished", "stopped": False,
            "tasks": [{"key": "T-01", "ticket_id": "PLAN-UNPUB-001", "task": {"title": "Step 1"}}],
        })
        ws = runtime.resolve_workspace(repo)
        with self.assertRaisesRegex(runtime.WorkflowError, "unfinished Strict Plan"):
            runtime.execute_run(ws, "Try write while unpublished plan exists.",
                                codex_cmd=[sys.executable, str(FAKE_APP)], timeout=15)


if __name__ == "__main__":
    unittest.main()
