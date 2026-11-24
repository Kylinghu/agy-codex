"""Cross-thread Maildir tests against the fake native App Server."""
import concurrent.futures
import time
import unittest
from unittest import mock

import test_app_server
import inbox

sa = test_app_server.sa


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.f = test_app_server.AppServerTests("test_app_server_uses_native_schema_and_independent_review")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.c = self.f.c
        self.ticket = self.f.f.ticket_id
        self.f.f.create(max_rounds=1)
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.pool.shutdown, wait=True)
        self.addCleanup(self.c.cancel.set)

    def wait_window(self, previous=None):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            state = self.c.load(self.ticket)
            if state.get("inbox_window") and state["inbox_window"] != previous and state.get("active_turn_id"):
                return state["inbox_window"]
            time.sleep(.02)
        self.fail("App Server did not open a live Turn window")

    def run_worker(self, **mode):
        self.f.mode(hold=True, **mode)
        return self.pool.submit(self.c.run, self.ticket)

    def test_queue_and_steer_run_before_single_independent_review(self):
        job = self.run_worker()
        self.wait_window()
        queued = inbox.publish(self.c, self.ticket, "queue", "Keep the existing input format.")
        steered = inbox.publish(self.c, self.ticket, "steer", "Use the fixture convention.")
        state = job.result(timeout=20)
        self.assertEqual(state["status"], "REVIEW", state)
        self.assertEqual(state["round"], 1)
        self.assertEqual(state["queue_turns"], 1)
        calls = self.f.calls("turn/start")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["params"]["threadId"], calls[1]["params"]["threadId"])
        self.assertIn(queued["text"], calls[1]["params"]["input"][0]["text"])
        messages = {m["id"]: m for m in self.c.packet(self.ticket)["inbox_messages"]}
        self.assertEqual(messages[queued["id"]]["status"], "turn_completed")
        self.assertEqual(messages[steered["id"]]["status"], "turn_completed")
        self.assertEqual(messages[steered["id"]]["delivery_action"], "steer")
        self.assertTrue(self.c.packet(self.ticket)["all_tests_passed"])
        from product import usage
        self.assertEqual(usage(self.c, state)["codex"], {"input_tokens": 24, "cached_input_tokens": 4, "output_tokens": 16})

    def test_only_exact_stale_steer_is_queued(self):
        job = self.run_worker(stale_steer=True)
        self.wait_window()
        msg = inbox.publish(self.c, self.ticket, "steer", "Keep the fixture positive.")
        state = job.result(timeout=20)
        self.assertEqual(state["status"], "REVIEW", state)
        self.assertEqual(len(self.f.calls("turn/start")), 2)
        actual = next(m for m in inbox.history(self.c, self.ticket) if m["id"] == msg["id"])
        self.assertEqual(actual["delivery_action"], "queue")
        self.assertEqual(actual["status"], "turn_completed")
        self.assertTrue(actual["stale_steer"])

    def test_other_invalid_request_is_not_replayed_as_queue(self):
        job = self.run_worker(other_steer_error=True)
        self.wait_window()
        msg = inbox.publish(self.c, self.ticket, "steer", "Try the fixture.")
        state = job.result(timeout=20)
        self.assertEqual(state["status"], "REVIEW", state)
        self.assertEqual(len(self.f.calls("turn/start")), 1)
        actual = next(m for m in inbox.history(self.c, self.ticket) if m["id"] == msg["id"])
        self.assertEqual(actual["status"], "rejected")

    def test_interrupt_preserves_session_and_defers_queued_input(self):
        job = self.run_worker()
        self.wait_window()
        msg = inbox.publish(self.c, self.ticket, "queue", "Do not lose this message.")
        stop = inbox.publish(self.c, self.ticket, "interrupt")
        state = job.result(timeout=20)
        self.assertEqual(state["status"], "ESCALATE", state)
        self.assertTrue(state["recoverable_interruption"])
        self.assertEqual(len(self.f.calls("turn/start")), 1)
        self.assertIsNotNone(state["session_id"])
        records = {m["id"]: m for m in inbox.history(self.c, self.ticket)}
        self.assertEqual(records[msg["id"]]["status"], "deferred")
        self.assertEqual(records[stop["id"]]["status"], "turn_interrupted")

    def test_late_interrupt_preserves_completed_final_round(self):
        # A late interrupt can be consumed by the final sealed-window drain.
        original = inbox.drain
        def drain(*args, **kwargs):
            queued, _ = original(*args, **kwargs)
            return queued, True
        with mock.patch.object(inbox, "drain", side_effect=drain):
            state = self.c.run(self.ticket)
        self.assertEqual(state["status"], "ESCALATE", state)
        self.assertTrue(state["recoverable_interruption"])
        self.assertEqual(sa.read_json(self.c._round_dir(state)/"summary.json")["exit_code"], 0)
        recovered = self.c.recover(self.ticket)
        self.assertEqual(recovered["status"], "REVIEW", recovered)
        self.assertEqual(recovered["round"], 1)
        self.assertEqual(len(self.f.calls("turn/start")), 1)

    def test_interrupt_error_before_terminal_notification_pauses_without_replay(self):
        job = self.run_worker(stale_interrupt=True)
        self.wait_window()
        msg = inbox.publish(self.c, self.ticket, "interrupt")
        state = job.result(timeout=20)
        self.assertEqual(state["status"], "ESCALATE", state)
        self.assertTrue(state["recoverable_interruption"])
        self.assertFalse(state["app_server_pending"])
        actual = next(m for m in inbox.history(self.c, self.ticket) if m["id"] == msg["id"])
        self.assertEqual(actual["status"], "deferred")
        self.assertEqual(sa.read_json(self.c._round_dir(state)/"summary.json")["exit_code"], 0)
        recovered = self.c.recover(self.ticket)
        self.assertEqual(recovered["status"], "REVIEW", recovered)
        self.assertEqual(len(self.f.calls("turn/start")), 1)

    def test_other_interrupt_error_retains_uncertain_submission(self):
        job = self.run_worker(other_interrupt_error=True)
        self.wait_window()
        msg = inbox.publish(self.c, self.ticket, "interrupt")
        state = job.result(timeout=20)
        self.assertEqual(state["status"], "ESCALATE", state)
        self.assertTrue(state["app_server_pending"])
        actual = next(m for m in inbox.history(self.c, self.ticket) if m["id"] == msg["id"])
        self.assertEqual(actual["status"], "delivery_unknown")
        self.assertEqual(len(self.f.calls("turn/start")), 1)

    def test_reconcile_updates_only_matching_accepted_ack(self):
        self.f.mode(ambiguous=True, not_loaded=True)
        state = self.c.run(self.ticket)
        self.assertTrue(state["app_server_pending"])
        tid = state["active_turn_id"]
        # Simulate ACKs durably saved before a transport break. Other/unknown
        # records must not be promoted by this Turn's terminal result.
        state["status"] = "RUNNING"
        window = inbox.open_window(self.c, state)
        messages = [inbox.publish(self.c, self.ticket, "queue", text)
                    for text in ("accepted", "other turn", "unknown")]
        for message in messages:
            inbox.claim(window, window/"pending"/(message["id"]+".json"))
        inbox.ack(self.c, messages[0], "rpc_accepted", turn_id=tid)
        inbox.ack(self.c, messages[1], "rpc_accepted", turn_id="different-turn")
        inbox.seal(self.c, state, window)
        state["status"] = "ESCALATE"
        self.c.save(state)
        recovered = self.c.recover(self.ticket)
        self.assertEqual(recovered["status"], "REVIEW", recovered)
        result = {m["id"]: m["status"] for m in self.c.packet(self.ticket)["inbox_messages"]}
        self.assertEqual(result[messages[0]["id"]], "turn_completed")
        self.assertEqual(result[messages[1]["id"]], "rpc_accepted")
        self.assertEqual(result[messages[2]["id"]], "delivery_unknown")
        self.assertEqual(len(self.f.calls("turn/start")), 1)

    def test_supplement_budget_is_finite_per_ticket(self):
        job = self.run_worker(hold_all=True)
        previous = None
        for i in range(inbox.MAX_QUEUE_TURNS+1):
            previous = self.wait_window(previous)
            msg = inbox.publish(self.c, self.ticket, "queue", f"Supplement {i}")
            inbox.publish(self.c, self.ticket, "steer", f"Finish Turn {i}")
        state = job.result(timeout=20)
        self.assertEqual(state["status"], "REVIEW", state)
        self.assertEqual(len(self.f.calls("turn/start")), inbox.MAX_QUEUE_TURNS+1)
        self.assertEqual(state["queue_turns"], inbox.MAX_QUEUE_TURNS)
        actual = next(m for m in inbox.history(self.c, self.ticket) if m["id"] == msg["id"])
        self.assertEqual(actual["status"], "deferred")

    def test_closed_window_cannot_be_recreated_by_a_late_writer(self):
        state = self.c.load(self.ticket)
        state.update(status="RUNNING", backend="app-server")
        window = inbox.open_window(self.c, state)
        original = inbox._metadata_io
        def race(operation):
            (window/"pending").rename(window/"sealed")
            return original(operation)
        with mock.patch.object(inbox, "_metadata_io", side_effect=race):
            with self.assertRaises(sa.WorkflowError):
                inbox.publish(self.c, self.ticket, "queue", "Too late")
        self.assertFalse((window/"pending").exists())
        self.assertEqual(list((window/"sealed").glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
