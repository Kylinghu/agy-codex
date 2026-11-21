"""Stages and results by default; optional bounded details, no timed log heartbeats."""
from __future__ import annotations

import asyncio
import contextlib
import queue
import time

from controller import WorkflowError, atomic_json, now

POLL_SECONDS = 0.2
DETAIL_SECONDS = 5.0
DETAIL_LIMIT = 12
PERSIST_SECONDS = 2.0


def validate_mode(mode):
    if mode not in ("stages", "detailed"):
        raise WorkflowError("progress must be stages (default) or detailed.")
    return mode


class ProgressFeed:
    def __init__(self, controller, ticket_id, round_number, mode="stages"):
        self.mode = validate_mode(mode)
        self.path = controller.ticket_dir(ticket_id) / "progress.json"
        self.started = time.monotonic()
        self.pending = queue.Queue(maxsize=1)
        self.files = set()
        self.producer = {"schema_version": 1, "ticket_id": ticket_id, "round": round_number, "display_mode": self.mode,
                         "stage": "worker", "commands_completed": 0, "commands_running": 0, "changed_file_count": 0,
                         "recent_files": [], "test_index": 0, "test_total": 0,
                         "tests_completed": 0, "tests_failed": 0, "tests_skipped": 0, "test_result": None}
        self.current = dict(self.producer)
        self.last_log = self.started
        self.last_persist = 0.0
        self.last_signature = None
        self.phase = "worker"
        self.detail_count = 0
        self.validation_announced = False

    def emit(self, event):
        """Called only by the controller thread; never call AGY from this thread."""
        kind = event.get("kind")
        if kind == "stage":
            for key in ("stage", "round", "test_total"):
                if key in event:
                    self.producer[key] = event[key]
        elif kind == "command":
            if event.get("completed"):
                self.producer["commands_completed"] += 1
                self.producer["commands_running"] = max(0, self.producer["commands_running"] - 1)
            else:
                self.producer["commands_running"] += 1
        elif kind == "files":
            for path in event.get("paths", []):
                if isinstance(path, str) and path:
                    self.files.add(path)
            self.producer["changed_file_count"] = len(self.files)
            self.producer["recent_files"] = ["".join(c if c.isprintable() else " " for c in name)[:80]
                                             for name in sorted(self.files)[-3:]]
        elif kind == "test":
            self.producer.update(test_index=event["index"], test_result=event["result"])
            if event["result"] == "SKIPPED":
                self.producer["tests_skipped"] += 1
            elif event["result"] in {"PASS", "FAIL"}:
                self.producer["tests_completed"] += 1
                self.producer["tests_failed"] += event["result"] == "FAIL"
        else:
            return
        self.producer["last_event_at"] = now()
        snapshot = dict(self.producer)
        # Only superseded display summaries are discarded; JSONL is untouched.
        with contextlib.suppress(queue.Empty):
            self.pending.get_nowait()
        self.pending.put_nowait(snapshot)

    def text(self):
        value = self.current
        elapsed = value.get("elapsed_seconds", 0)
        if not value.get("running", True):
            outcome = "准备复审" if value.get("result_status") == "REVIEW" else "已结束，结果需要处理"
            return (f"本轮执行{outcome}；已完成 {value['commands_completed']} 条命令，记录 {value['changed_file_count']} 个变更文件；"
                    f"独立验收通过 {value['tests_completed'] - value['tests_failed']} 项，失败 {value['tests_failed']} 项，跳过 {value['tests_skipped']} 项。")
        if value["stage"] == "validation":
            return (f"独立验收 {value['test_index']}/{value['test_total']}，已完成 {value['tests_completed']} 项，"
                    f"失败 {value['tests_failed']} 项，跳过 {value['tests_skipped']} 项；本轮已用 {elapsed} 秒。")
        if value["stage"] == "evidence":
            return (f"正在整理复审证据；已完成 {value['tests_completed']}/{value['test_total']} 项独立验收，"
                    f"失败 {value['tests_failed']} 项，跳过 {value['tests_skipped']} 项。")
        names = "，近期文件：" + "、".join(value["recent_files"]) if value["recent_files"] else ""
        active = f"正在执行 {value['commands_running']} 条命令，" if value["commands_running"] else ""
        return (f"Codex 仍在执行，已用 {elapsed} 秒；{active}已完成 {value['commands_completed']} 条命令，"
                f"记录 {value['changed_file_count']} 个变更文件{names}。")

    def publish(self, phase_fn, log_fn, *, finished=None):
        """Persist live facts; emit only selected changes, never timed log heartbeats."""
        with contextlib.suppress(queue.Empty):
            self.current = self.pending.get_nowait()
        tick = time.monotonic()
        self.current.update(elapsed_seconds=int(tick - self.started), updated_at=now(), running=finished is None)
        if finished is not None:
            self.current["result_status"] = finished
        stage_changed = self.phase != self.current["stage"]
        signature = tuple(str(self.current.get(key)) for key in
                          ("stage", "commands_completed", "commands_running", "changed_file_count", "test_index", "test_result"))
        activity = bool(self.current["commands_completed"] or self.current["commands_running"] or self.current["changed_file_count"] or self.current["test_index"])
        first_validation = (self.current["stage"] == "validation" and self.current["test_index"] > 0
                            and not self.validation_announced)
        details_due = (self.mode == "detailed" and activity and signature != self.last_signature
                       and (self.last_signature is None or tick - self.last_log >= DETAIL_SECONDS)
                       and self.detail_count < DETAIL_LIMIT)
        # The first actual validation update is a stage announcement, not a
        # detail. It remains visible even after a long Worker's detail budget.
        should_log = first_validation or details_due
        if finished is not None:
            should_log = activity
        if finished is not None or stage_changed or should_log or tick - self.last_persist >= PERSIST_SECONDS:
            atomic_json(self.path, self.current)
            self.last_persist = tick
        if stage_changed:
            self.phase = self.current["stage"]
            phase_fn({"validation": "独立验收", "evidence": "整理复审证据"}.get(self.phase, "Codex 执行"))
        if should_log:
            log_fn(self.text())
            self.last_log, self.last_signature = tick, signature
            if first_validation:
                self.validation_announced = True
            elif finished is None:
                self.detail_count += 1


async def run_controller(controller, method, ticket_id, phase_fn, log_fn, *, progress_mode="stages"):
    """Display progress while retaining ownership until subprocess cleanup ends."""
    state = controller.load(ticket_id)
    feed = ProgressFeed(controller, ticket_id, state["round"] + (method == controller.run), mode=progress_mode)
    previous = controller.progress_callback
    controller.progress_callback = feed.emit
    controller.cancel.clear()
    job = asyncio.get_running_loop().run_in_executor(None, method, ticket_id)
    try:
        while True:
            done, _ = await asyncio.wait({job}, timeout=POLL_SECONDS)
            if done:
                result = job.result()
                feed.publish(phase_fn, log_fn, finished=result["status"])
                return result
            feed.publish(phase_fn, log_fn)
    except BaseException:
        controller.cancel.set()
        with contextlib.suppress(OSError):
            feed.current.update(running=not job.done(), result_status="STOPPING", updated_at=now())
            atomic_json(feed.path, feed.current)
        # Retain the workflow lock and callback until real cleanup finishes. A
        # repeated host cancellation must not strand the owned executor thread.
        while not job.done():
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.shield(job)
        with contextlib.suppress(OSError):
            feed.current.update(running=False, result_status="INTERRUPTED", updated_at=now())
            atomic_json(feed.path, feed.current)
        raise
    finally:
        controller.progress_callback = previous
