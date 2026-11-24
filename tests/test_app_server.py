"""Real subprocess tests against an explicitly fake Codex App Server."""
import json
from pathlib import Path
import sys
import unittest
import test_protocol

sa = test_protocol.sa
FAKE = Path(__file__).with_name("fake_app_server.py").resolve()


class AppServerTests(unittest.TestCase):
    def setUp(self):
        self.f = test_protocol.ProtocolTests("test_pass_has_independent_tests_diff_and_persistent_session")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.c = sa.Controller(self.f.repo, codex_cmd=[sys.executable, str(FAKE)], timeout=15)
        self.f.controller = self.c
        self.configure()

    def configure(self, **extra):
        sa.atomic_json(self.f.global_config, {"backend": "app-server", **extra})

    def mode(self, **extra):
        sa.atomic_json(self.f.repo/".git"/"fake-app-control.json", extra)

    def calls(self, method=None):
        path = self.f.repo/".git"/"fake-app-calls.jsonl"
        calls = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
        return [x for x in calls if x.get("method") == method] if method else calls

    def test_app_server_uses_native_schema_and_independent_review(self):
        self.mode(approval=True)
        self.f.create(codex={"model": "fixture-model", "reasoning_effort": "low", "model_provider": "openai"})
        state = self.c.run(self.f.ticket_id)
        self.assertEqual(state["status"], "REVIEW", state)
        self.assertEqual(state["backend"], "app-server")
        self.assertEqual(state["model_provider"], "openai")
        self.assertIsNone(state["child_pid"])
        self.assertFalse(state["app_server_pending"])
        self.assertEqual(self.calls("turn/start")[0]["params"]["outputSchema"], sa.WORKER_SCHEMA)
        self.assertEqual(self.calls("turn/start")[0]["params"]["effort"], "low")
        self.assertIn({"id": "server-approval", "result": {"decision": "decline"}}, self.calls())
        packet = self.c.packet(self.f.ticket_id)
        self.assertTrue(packet["all_tests_passed"])
        self.assertEqual(packet["worker_report"]["outcome"], "done")
        self.assertNotIn("NOT_FOR_LOGS", (self.c._round_dir(state)/"model-catalog.json").read_text())
        final = self.c.verdict(self.f.ticket_id, self.f.review(), source="test")
        self.assertEqual(final["status"], "PASS")

    def test_resume_preserves_thread_and_allows_model_upgrade(self):
        self.f.create(codex={"model": "fixture-model", "reasoning_effort": "low"})
        first = self.c.run(self.f.ticket_id)
        self.assertEqual(first["status"], "REVIEW", first)
        self.c.verdict(self.f.ticket_id, self.f.review("REVISE"), source="test")
        second = self.c.run(self.f.ticket_id, codex={"model": "fixture-upgrade", "reasoning_effort": "high"})
        self.assertEqual(second["status"], "REVIEW", second)
        self.assertEqual(second["session_id"], first["session_id"])
        from product import usage
        self.assertEqual(usage(self.c, second)["codex"], {"input_tokens": 24, "cached_input_tokens": 4, "output_tokens": 16})
        self.assertEqual(self.calls("thread/resume")[0]["params"]["threadId"], first["session_id"])
        self.assertEqual(self.calls("turn/start")[1]["params"]["model"], "fixture-upgrade")

    def test_auto_fallback_only_before_thread_submission(self):
        self.configure(backend="auto")
        self.mode(init_error=True)
        self.f.create()
        state = self.c.run(self.f.ticket_id)
        self.assertEqual(state["status"], "REVIEW", state)
        self.assertEqual(state["backend"], "exec")
        self.assertEqual(len(self.f.calls()), 1)
        self.assertEqual(self.calls("turn/start"), [])
        self.assertTrue((self.c._round_dir(state)/"backend-fallback.json").exists())

    def test_ambiguous_turn_is_reconciled_without_replaying(self):
        self.configure(backend="auto")
        self.mode(ambiguous=True, not_loaded=True)
        self.f.create(codex={"model_provider": "openai"})
        state = self.c.run(self.f.ticket_id)
        self.assertEqual(state["status"], "ESCALATE", state)
        self.assertTrue(state["app_server_pending"])
        self.assertTrue(state["recoverable_interruption"])
        self.assertEqual(len(self.calls("turn/start")), 1)
        self.assertEqual(self.f.calls(), [])
        recovered = self.c.recover(self.f.ticket_id)
        self.assertEqual(recovered["status"], "REVIEW", recovered)
        self.assertEqual(recovered["round"], 1)
        self.assertFalse(recovered["app_server_pending"])
        self.assertEqual(self.calls("thread/resume")[0]["params"]["threadId"], state["session_id"])
        self.assertEqual(self.calls("thread/resume")[0]["params"]["modelProvider"], "openai")
        self.assertEqual(sa.read_json(self.c._round_dir(recovered)/"summary.json")["usage"]["input_tokens"], 12)
        self.assertEqual(len(self.calls("turn/start")), 1)
        self.assertEqual(self.f.calls(), [])

    def test_provider_allowlist_prevents_turn(self):
        self.configure(routing={"allowed_providers": ["openai"]})
        self.mode(provider="proxy")
        self.f.create()
        state = self.c.run(self.f.ticket_id)
        self.assertEqual(state["status"], "ESCALATE", state)
        self.assertEqual(self.calls("turn/start"), [])
        self.assertEqual(self.f.calls(), [])

    def test_explicit_disallowed_provider_fails_before_startup(self):
        self.configure(routing={"allowed_providers": ["openai"]})
        self.f.create(codex={"model_provider": "proxy"})
        with self.assertRaisesRegex(sa.WorkflowError, "allowlist"):
            self.c.run(self.f.ticket_id)
        self.assertEqual(self.c.load(self.f.ticket_id)["round"], 0)
        self.assertEqual(self.calls(), [])

    def test_custom_tier_names_do_not_require_implicit_standard(self):
        from routing import select
        tiers = {"economy": {"model": "fixture-model"}, "heavy": {"model": "fixture-upgrade"}}
        self.configure(routing={"strategy": "auto", "tiers": tiers})
        self.assertEqual(select(self.c.root), {})
        self.assertEqual(select(self.c.root, "heavy"), tiers["heavy"])
        state = self.f.create(codex={"model": "fixture-model"})
        self.assertEqual(state["codex_requested"], tiers["economy"])
        with self.assertRaisesRegex(sa.WorkflowError, "Unknown configured Codex tier"):
            select(self.c.root, "standard")
        self.configure(routing={"tiers": tiers, "default_tier": "unknown"})
        with self.assertRaisesRegex(sa.WorkflowError, "default_tier"):
            select(self.c.root)

    def test_probe_exposes_catalog_and_quota_without_turn(self):
        from routing import probe, select
        self.configure(routing={"strategy": "economy", "tiers": {
            "economy": {"model": "fixture-model", "reasoning_effort": "low"}}})
        self.assertEqual(select(self.c.root)["model"], "fixture-model")
        result = probe(self.c)
        self.assertEqual(result["rate_limits"]["rateLimits"]["primary"]["usedPercent"], 4)
        self.assertEqual(result["default_provider"], "openai")
        self.assertEqual(self.calls("model/list")[0]["params"]["modelProvider"], "openai")
        self.assertTrue(result["catalog_is_not_entitlement"])
        self.assertNotIn("NOT_FOR_LOGS", json.dumps(result))
        self.assertEqual(self.calls("turn/start"), [])


if __name__ == "__main__":
    unittest.main()
