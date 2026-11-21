"""Prove progress arrives before subprocess exit and cancellation releases ownership safely."""
import asyncio
import contextlib
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/agy-codex/scripts"
sys.path.insert(0, str(SCRIPTS))
import agy_adapter
import product
import progress
import test_protocol


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.f = test_protocol.ProtocolTests("test_pass_has_independent_tests_diff_and_persistent_session")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.c = self.f.controller
        self.f.create()
        self.logs, self.phases = [], []
        self.main_thread = threading.get_ident()
        self.release = self.f.repo / ".git/fake-progress-release"
        self.path = self.c.ticket_dir(self.f.ticket_id) / "progress.json"
        patcher = mock.patch.object(progress, "POLL_SECONDS", 0.02)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def reviewer(self, prompt, schema):
        return self.f.review()

    async def invoke_async(self, logger=None, mode=None):
        options = {"action": "continue"}
        if mode is not None:
            options["progress"] = mode
        return await agy_adapter.run_workflow(
            agent_fn=lambda **kwargs: self.reviewer, phase_fn=self.phases.append,
            log_fn=logger or self.logs.append, workflow_args=options,
            controller=self.c, preflight=False)

    async def observe_live(self, job, *, marker=None, stage="worker", test_index=None, has_activity=False):
        """Readiness is a process handshake, independent of display cadence."""
        async def observe():
            while True:
                if job.done():
                    self.fail("Workflow ended before observing a live process: " + repr(await job))
                state = self.c.load(self.f.ticket_id)
                if (marker is None or marker.exists()) and self.path.exists():
                    saved = json.loads(self.path.read_text())
                    if (state["status"] == "RUNNING" and state["session_id"]
                            and saved["ticket_id"] == self.f.ticket_id and saved["round"] == state["round"]
                            and saved["stage"] == stage and saved["running"]
                            and (not has_activity or saved["commands_completed"] + saved["changed_file_count"] > 0)
                            and (test_index is None or (saved["test_index"] == test_index and saved["test_result"] == "RUNNING"))
                            and test_protocol.sa.pid_alive(state["child_pid"])):
                        return state, saved
                await asyncio.sleep(0.01)
        return await asyncio.wait_for(observe(), timeout=self.c.timeout)

    async def cleanup_job(self, job):
        if not job.done():
            job.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await job

    def test_progress_and_status_are_visible_while_worker_is_still_alive(self):
        self.f.mode("progress-wait")
        async def scenario():
            job = asyncio.create_task(self.invoke_async())
            try:
                state, saved = await self.observe_live(job, marker=self.f.repo / ".git/fake-progress-ready", has_activity=True)
                self.assertGreater(saved["commands_completed"] + saved["changed_file_count"], 0)
                self.assertEqual(saved["recent_files"], ["value.py"])
                self.assertEqual(saved["display_mode"], "stages")
                status = product.report(self.c, state)
                self.assertIsNone(status["snapshot_current"])
                self.assertFalse(status["verified"])
                self.assertEqual(self.logs, [])
                self.assertTrue(any("Codex 实现" in phase for phase in self.phases))
                self.release.write_text("release")
                self.assertTrue((await job)["verified"])
            finally:
                await self.cleanup_job(job)
        asyncio.run(scenario())
        saved = json.loads(self.path.read_text())
        self.assertFalse(saved["running"])
        self.assertEqual(saved["tests_completed"], 1)
        self.assertEqual(saved["tests_skipped"], 0)

    def test_event_bursts_are_coalesced_without_forwarding_model_or_command_text(self):
        self.f.mode("burst-wait")
        def logger(message):
            self.assertEqual(threading.get_ident(), self.main_thread)
            self.logs.append(message)
            if "Codex 仍在执行" in message:
                self.release.write_text("release")
        result = asyncio.run(self.invoke_async(logger, mode="detailed"))
        self.assertTrue(result["verified"])
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["commands_completed"], 101)
        rendered = "\n".join(self.logs) + json.dumps(saved)
        self.assertNotIn("DO_NOT_DISPLAY", rendered)
        details = [line for line in self.logs if "Codex 仍在执行" in line or "本轮执行" in line]
        self.assertLessEqual(len(details), 3)
        raw = (self.c.ticket_dir(self.f.ticket_id) / "rounds/01/codex.jsonl").read_text()
        self.assertIn("DO_NOT_DISPLAY_REASONING", raw)
        self.assertIn("DO_NOT_DISPLAY_STDOUT", raw)

    def test_silent_worker_has_queryable_status_without_heartbeat_logs(self):
        self.f.mode("silent-wait")
        async def scenario():
            job = asyncio.create_task(self.invoke_async())
            try:
                state, saved = await self.observe_live(job)
                self.assertEqual(saved["commands_completed"], 0)
                self.assertEqual(saved["changed_file_count"], 0)
                self.assertEqual(self.logs, [])
                self.assertEqual(product.report(self.c, state)["progress"]["round"], state["round"])
                self.release.write_text("release")
                self.assertTrue((await job)["verified"])
            finally:
                await self.cleanup_job(job)
        asyncio.run(scenario())

    def test_display_failure_cleans_worker_and_continue_resumes_exact_session(self):
        self.f.mode("progress-wait")
        def broken_logger(message):
            if "Codex 仍在执行" in message:
                raise RuntimeError("Injected AGY display failure")
        with self.assertRaisesRegex(RuntimeError, "display failure"):
            asyncio.run(self.invoke_async(broken_logger, mode="detailed"))
        state = self.c.load(self.f.ticket_id)
        self.assertEqual(state["status"], "ESCALATE")
        self.assertTrue(state["recoverable_interruption"])
        self.assertFalse(test_protocol.sa.pid_alive(state["last_child_pid"]))
        self.assertFalse(json.loads(self.path.read_text())["running"])
        self.assertIsNone(self.c.progress_callback)
        session = state["session_id"]
        self.f.mode("success")
        result = asyncio.run(self.invoke_async())
        self.assertTrue(result["verified"])
        self.assertEqual(self.f.calls()[1]["requested_session"], session)

    def test_host_cancellation_waits_for_cleanup_before_workflow_lock_is_released(self):
        self.f.mode("progress-wait")
        async def scenario():
            job = asyncio.create_task(self.invoke_async())
            try:
                state, _ = await self.observe_live(job, marker=self.f.repo / ".git/fake-progress-ready")
                job.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await job
                self.assertFalse(test_protocol.sa.pid_alive(state["child_pid"]))
                self.assertFalse(json.loads(self.path.read_text())["running"])
                with test_protocol.sa.worktree_lock(self.c.root / "workflow.lock"):
                    self.assertIsNone(self.c.progress_callback)
            finally:
                await self.cleanup_job(job)
        asyncio.run(scenario())

    def test_independent_validation_progress_arrives_before_test_process_finishes(self):
        # A separate observer releases the real child; log delivery is not a
        # synchronization primitive and can precede Windows process readiness.
        state = self.c.load(self.f.ticket_id)
        # Deliberately delay readiness beyond the former 12 * 20 ms window.
        test_script = ("from pathlib import Path; import time; time.sleep(.35); "
                       "Path('.git/test-ready').write_text('ready'); "
                       "deadline=time.monotonic()+15\n"
                       "while not Path('.git/test-release').exists() and time.monotonic()<deadline: time.sleep(.01)\n"
                       "assert Path('.git/test-release').exists()")
        state["spec"]["test_commands"] = [["{python}", "-c", test_script]]
        # Set the contract before execution, just as create(test_commands=...) would.
        self.c.save(state)
        test_protocol.sa.atomic_json(self.c.ticket_dir(self.f.ticket_id) / "contract.json", state["spec"])
        async def scenario():
            job = asyncio.create_task(self.invoke_async())
            try:
                state, saved = await self.observe_live(job, marker=self.f.repo / ".git/test-ready",
                                                      stage="validation", test_index=1)
                self.assertEqual(saved["tests_completed"], 0)
                self.assertIn("独立验收", self.phases)
                self.assertEqual(sum(message.startswith("独立验收 1/1") for message in self.logs), 1)
                self.assertFalse(any("Codex 仍在执行" in message for message in self.logs))
                self.assertEqual(product.report(self.c, state)["progress"]["test_result"], "RUNNING")
                (self.f.repo / ".git/test-release").write_text("release")
                self.assertTrue((await job)["verified"])
            finally:
                await self.cleanup_job(job)
        asyncio.run(scenario())
        self.assertEqual(len(self.f.calls()), 1)

    def test_default_long_running_progress_stays_local_until_stage_and_result(self):
        clock = [0.0]
        with mock.patch.object(progress, "time", SimpleNamespace(monotonic=lambda: clock[0])):
            feed = progress.ProgressFeed(self.c, self.f.ticket_id, 1)
            for tick in (0, 5, 20, 60, 600, 3600):
                clock[0] = tick
                feed.emit({"kind": "command", "completed": True})
                feed.publish(self.phases.append, self.logs.append)
            self.assertEqual(self.logs, [])
            saved = json.loads(self.path.read_text())
            self.assertEqual((saved["commands_completed"], saved["elapsed_seconds"]), (6, 3600))
            feed.emit({"kind": "stage", "stage": "validation", "test_total": 1})
            feed.emit({"kind": "test", "index": 1, "result": "RUNNING"})
            feed.publish(self.phases.append, self.logs.append)
            self.assertEqual(len(self.logs), 1)
            clock[0] += 3600
            feed.publish(self.phases.append, self.logs.append)
            self.assertEqual(len(self.logs), 1)
            feed.emit({"kind": "test", "index": 1, "result": "PASS"})
            feed.emit({"kind": "stage", "stage": "evidence"})
            feed.publish(self.phases.append, self.logs.append, finished="REVIEW")
            self.assertEqual(len(self.logs), 2)
            self.assertIn("通过 1 项", self.logs[-1])

    def test_detail_budget_and_cadence_cannot_hide_first_validation_or_result(self):
        clock = [0.0]
        with mock.patch.object(progress, "time", SimpleNamespace(monotonic=lambda: clock[0])):
            feed = progress.ProgressFeed(self.c, self.f.ticket_id, 1, mode="detailed")
            feed.emit({"kind": "command", "completed": True})
            feed.publish(self.phases.append, self.logs.append)
            self.assertEqual(len(self.logs), 1)
            clock[0] = .01
            feed.emit({"kind": "command", "completed": True})
            feed.publish(self.phases.append, self.logs.append)
            self.assertEqual(len(self.logs), 1)
            clock[0] = progress.DETAIL_SECONDS
            feed.publish(self.phases.append, self.logs.append)
            self.assertEqual(len(self.logs), 2)
            clock[0] += 3600
            feed.publish(self.phases.append, self.logs.append)
            self.assertEqual(len(self.logs), 2)  # Silence consumes no unused detail budget.
            for _ in range(progress.DETAIL_LIMIT - 2):
                clock[0] += progress.DETAIL_SECONDS + .1
                feed.emit({"kind": "command", "completed": True})
                feed.publish(self.phases.append, self.logs.append)
            self.assertEqual(len(self.logs), progress.DETAIL_LIMIT)
            clock[0] += .01
            feed.emit({"kind": "command", "completed": True})
            feed.publish(self.phases.append, self.logs.append)
            self.assertEqual(len(self.logs), progress.DETAIL_LIMIT)
            # Entering the stage and observing its first actual test can arrive
            # on separate polls. Both must bypass the exhausted detail budget.
            feed.emit({"kind": "stage", "stage": "validation", "test_total": 1})
            feed.publish(self.phases.append, self.logs.append)
            self.assertIn("独立验收", self.phases)
            feed.emit({"kind": "test", "index": 1, "result": "RUNNING"})
            feed.publish(self.phases.append, self.logs.append)
            self.assertEqual(len(self.logs), progress.DETAIL_LIMIT + 1)
            self.assertTrue(self.logs[-1].startswith("独立验收 1/1"))
            clock[0] += 3600
            feed.emit({"kind": "command", "completed": True})
            feed.publish(self.phases.append, self.logs.append)
            self.assertEqual(len(self.logs), progress.DETAIL_LIMIT + 1)
            feed.emit({"kind": "test", "index": 1, "result": "PASS"})
            feed.publish(self.phases.append, self.logs.append, finished="REVIEW")
            self.assertEqual(len(self.logs), progress.DETAIL_LIMIT + 2)
            self.assertIn("通过 1 项", self.logs[-1])

    def test_unknown_progress_preference_is_rejected_before_execution(self):
        with self.assertRaisesRegex(test_protocol.sa.WorkflowError, "progress must be"):
            asyncio.run(self.invoke_async(mode="verbose"))
        self.assertEqual(self.c.load(self.f.ticket_id)["round"], 0)
        self.assertEqual(self.f.calls(), [])
        self.assertFalse(self.path.exists())

    def test_skipped_validation_is_never_counted_as_completed_or_passed(self):
        feed = progress.ProgressFeed(self.c, self.f.ticket_id, 1)
        feed.emit({"kind": "stage", "stage": "validation", "test_total": 2})
        feed.emit({"kind": "test", "index": 1, "result": "SKIPPED"})
        feed.emit({"kind": "test", "index": 2, "result": "SKIPPED"})
        feed.publish(self.phases.append, self.logs.append, finished="REVIEW")
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["tests_completed"], 0)
        self.assertEqual(saved["tests_skipped"], 2)
        self.assertIn("通过 0 项", self.logs[-1])
        self.assertIn("跳过 2 项", self.logs[-1])
        self.assertNotIn("仍在执行", self.logs[-1])


if __name__ == "__main__":
    unittest.main()
