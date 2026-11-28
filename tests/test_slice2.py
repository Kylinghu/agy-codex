"""Slice 2 tests: Strict slimming, plugin read-only reviewer, snapshot reduction, and model catalog TTL cache."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills/agy-codex/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agy_adapter
import controller as sa
import routing
import runtime
import smoke
import test_protocol

FAKE_APP_SERVER = Path(__file__).resolve().parent / "fake_app_server.py"


class Slice2SlimmingTests(unittest.TestCase):
    def setUp(self):
        self.f = test_protocol.ProtocolTests("test_pass_has_independent_tests_diff_and_persistent_session")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_fast_preflight_skips_full_doctor_unless_requested(self):
        self.f.create()
        async def reviewer(prompt, schema):
            return self.f.review()

        with mock.patch.object(agy_adapter, "doctor", side_effect=AssertionError("full doctor should not run")) as doc_mock:
            result = asyncio.run(agy_adapter.run_workflow(
                agent_fn=lambda **kw: reviewer,
                phase_fn=lambda v: None,
                log_fn=lambda v: None,
                workflow_args={"ticket": self.f.ticket_id},
                controller=self.f.controller,
                preflight=True,
            ))
            self.assertEqual(result["status"], "PASS", result)
            self.assertEqual(doc_mock.call_count, 0)
            doctor_json = sa.read_json(self.f.controller.ticket_dir(self.f.ticket_id) / "doctor.json")
            self.assertEqual(doctor_json["mode"], "fast")
            self.assertTrue(doctor_json["static_checks_passed"])

    def test_snapshot_calls_reduced_in_create_and_prepare_review(self):
        orig_snapshot = sa.Controller.snapshot
        calls = []

        def counting_snapshot(controller_self, *args, **kwargs):
            calls.append(args)
            return orig_snapshot(controller_self, *args, **kwargs)

        with mock.patch.object(sa.Controller, "snapshot", autospec=True, side_effect=counting_snapshot):
            self.f.create()
            # create() with unchanged files should scan once (not twice).
            self.assertEqual(len(calls), 1, f"create() scanned {len(calls)} times")

            calls.clear()
            state = self.f.controller.run(self.f.ticket_id)
            self.assertEqual(state["status"], "REVIEW", state)
            # run() scans 3 times (pre-run check, before tests, after tests) instead of 4.
            self.assertEqual(len(calls), 3, f"run()/_prepare_review() scanned {len(calls)} times")

    def test_config_reviewer_type_override_is_respected(self):
        self.f.create()
        sa.atomic_json(self.f.global_config, {"reviewer": {"type_name": "team-ro-reviewer"}})
        seen = []

        async def reviewer(prompt, schema):
            return self.f.review()

        def factory(**opts):
            seen.append(opts)
            return reviewer

        result = asyncio.run(agy_adapter.run_workflow(
            agent_fn=factory, phase_fn=lambda v: None, log_fn=lambda v: None,
            workflow_args={"ticket": self.f.ticket_id}, controller=self.f.controller, preflight=False,
        ))
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(seen[0]["type_name"], "team-ro-reviewer")
        self.assertEqual(
            self.f.controller.load(self.f.ticket_id)["last_review"]["reviewer_source"],
            "agy-team-ro-reviewer",
        )

    def test_smoke_verify_accepts_plugin_reviewer_and_rejects_self_non_independent(self):
        self.f.create()
        async def reviewer(prompt, schema):
            return self.f.review()

        result = asyncio.run(agy_adapter.run_workflow(
            agent_fn=lambda **kw: reviewer, phase_fn=lambda v: None, log_fn=lambda v: None,
            workflow_args={"ticket": self.f.ticket_id}, controller=self.f.controller, preflight=False,
        ))
        self.assertEqual(result["status"], "PASS", result)
        ok_res = smoke.verify(self.f.repo, self.f.ticket_id)
        self.assertTrue(ok_res["passed"], ok_res)
        self.assertTrue(ok_res["agy_review"], ok_res)

        # Mutate stored reviewer_source to non-independent self review; verify must report failed.
        tpath = self.f.controller.ticket_dir(self.f.ticket_id) / "ticket.json"
        state = sa.read_json(tpath)
        state["last_review"]["reviewer_source"] = "agy-self-non-independent"
        sa.atomic_json(tpath, state)
        bad_res = smoke.verify(self.f.repo, self.f.ticket_id)
        self.assertFalse(bad_res["agy_review"])
        self.assertFalse(bad_res["passed"])

    def test_install_script_copies_plugin_reviewer_agent(self):
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td) / "demo-project"
            project_dir.mkdir()
            proc = subprocess.run(
                [sys.executable, str(REPO_ROOT / "install.py"), "--project", str(project_dir)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            reviewer_md = project_dir / ".agents" / "plugins" / "agy-codex" / "agents" / "agy-codex-reviewer.md"
            self.assertTrue(reviewer_md.is_file(), "agents/agy-codex-reviewer.md was not installed")
            text = reviewer_md.read_text(encoding="utf-8")
            self.assertIn("subagent: true", text)
            self.assertIn("read_file", text)

    def test_user_cli_inbox_accepts_message_flag_alias(self):
        import inbox as inbox_mod
        import user_cli
        ws = runtime.resolve_workspace(self.f.repo)
        rid = "RUN-20261128-000001-abcdef"
        inbox_dir = runtime.run_dir_for(ws, rid) / "inbox"
        state = {
            "schema_version": 1,
            "run_id": rid,
            "owner_kind": "lightweight",
            "ticket_id": None,
            "round": 1,
            "cwd": str(ws.root_path),
            "is_git": True,
            "backend": "app-server",
            "status": "running",
            "inbox_window": None,
            "inbox_dir": str(inbox_dir),
            "artifact_dir": str(runtime.run_dir_for(ws, rid)),
            "started_at": runtime.now(),
            "started_ns": time.time_ns(),
        }
        runtime.save_run(ws, state, "started")
        target = inbox_mod.InboxTarget(
            inbox_dir=inbox_dir,
            owner_id=rid,
            load_state=lambda: runtime.load_run(ws, rid),
            save_state=lambda s, ev=None: runtime.save_run(ws, s, ev),
            round_number=1,
        )
        inbox_mod.open_window(target, state)
        rc = user_cli.main(["inbox", "steer", "--cwd", str(ws.root_path), "--message", "Add tag", "--json"])
        self.assertEqual(rc, 0)
        hist = inbox_mod.history(target)
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["text"], "Add tag")


class Slice2ModelCatalogCacheTests(unittest.TestCase):
    def setUp(self):
        self.f = test_protocol.ProtocolTests("test_pass_has_independent_tests_diff_and_persistent_session")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.codex_home = Path(self.f.temp.name) / "codex-home"
        self.codex_home.mkdir()
        self.env_patch = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home)})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.ws = runtime.resolve_workspace(self.f.repo)
        self.fake_cmd = [sys.executable, str(FAKE_APP_SERVER)]

    def _rpc_count(self):
        path = self.f.repo / ".git" / "fake-app-calls.jsonl"
        if not path.is_file():
            return 0
        return len([l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()])

    def test_probe_caches_results_and_invalidates_on_refresh_or_config_change(self):
        first = routing.probe(self.ws, codex_cmd=self.fake_cmd)
        self.assertFalse(first["from_cache"])
        first_calls = self._rpc_count()
        self.assertGreater(first_calls, 0)

        # Second call hits cache with zero new RPC calls.
        second = routing.probe(self.ws, codex_cmd=self.fake_cmd)
        self.assertTrue(second["from_cache"])
        self.assertEqual(self._rpc_count(), first_calls)
        self.assertEqual(second["models"], first["models"])

        # Explicit refresh bypasses cache.
        refreshed = routing.probe(self.ws, codex_cmd=self.fake_cmd, refresh=True)
        self.assertFalse(refreshed["from_cache"])
        after_refresh_calls = self._rpc_count()
        self.assertGreater(after_refresh_calls, first_calls)

        # Changing CODEX_HOME/config.toml invalidates cache automatically.
        (self.codex_home / "config.toml").write_text('model = "gpt-test"\n', encoding="utf-8")
        after_cfg = routing.probe(self.ws, codex_cmd=self.fake_cmd)
        self.assertFalse(after_cfg["from_cache"])
        self.assertGreater(self._rpc_count(), after_refresh_calls)

        # Expiring cached_at triggers fresh RPC call.
        cache_path = self.ws.state_dir / "model-catalog-cache.json"
        raw = runtime.read_json(cache_path)
        raw["cached_at"] = time.time() - 9999
        runtime.atomic_json(cache_path, raw)
        calls_before_expiry = self._rpc_count()
        expired = routing.probe(self.ws, codex_cmd=self.fake_cmd)
        self.assertFalse(expired["from_cache"])
        self.assertGreater(self._rpc_count(), calls_before_expiry)


if __name__ == "__main__":
    unittest.main()
