"""Owned stdio JSON-RPC transport for Codex App Server v2.

Decoupled from Strict Controller/Ticket/WORKER_SCHEMA. Shared by lightweight runs
and Strict rounds. No request after turn submission is automatically replayed
in another backend.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import runtime
from runtime import (
    Interrupted,
    WindowsJob,
    WorkflowError,
    atomic_json,
    now,
    resolve_codex,
    stop_process,
)


class StartupUnavailable(WorkflowError):
    """Initialization failed, before sending a Thread/Turn request."""


class RpcError(WorkflowError):
    def __init__(self, method, error):
        self.code = error.get("code")
        self.message = str(error.get("message", "RPC error"))
        super().__init__(f"{method}: {self.code}: {self.message}")


class Client:
    def __init__(self, cwd, launcher, directory, *, requested=None, timeout=900,
                 cancel=None, on_pid=None):
        self.cwd = Path(cwd)
        self.directory = Path(directory)
        self.argv = [*launcher, "app-server", "--listen", "stdio://"]
        provider = (requested or {}).get("model_provider")
        if provider:
            self.argv += ["-c", "model_provider=" + json.dumps(provider)]
        self.deadline = time.monotonic() + (timeout or 900)
        self.cancel = cancel or threading.Event()
        self.on_pid = on_pid
        self.incoming = queue.Queue(maxsize=256)
        self.outgoing = queue.Queue()
        self.stop = threading.Event()
        self.seq = 0
        self.p = self.reader = self.writer = self.err = self.trace = None
        self.notify = lambda event: None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.err = (self.directory / "app-server.stderr.log").open("wb")
        initializing = False
        try:
            gated = os.name == "nt"
            argv = [sys.executable, "-B", str(Path(runtime.__file__).resolve()),
                    "_child-gate", json.dumps(self.argv)] if gated else self.argv
            opts = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if gated else {"start_new_session": True}
            self.p = subprocess.Popen(
                argv, cwd=self.cwd, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.err, **opts,
            )
            if gated:
                self.p._superagent_job = WindowsJob(self.p)
            if self.on_pid:
                self.on_pid(self.p.pid, True)
            self.trace = (self.directory / "rpc.jsonl").open("a", encoding="utf-8")
            self.reader = threading.Thread(target=self._read, daemon=True)
            self.writer = threading.Thread(target=self._write, args=(gated,), daemon=True)
            self.reader.start(); self.writer.start()
            initializing = True
            self.call("initialize", {"clientInfo": {"name": "agy-codex", "version": runtime.VERSION}}, timeout=20)
            self.send({"method": "initialized", "params": {}})
            return self
        except (Exception, KeyboardInterrupt) as exc:
            self.close()
            if isinstance(exc, (Interrupted, KeyboardInterrupt)) or (self.p is not None and not initializing):
                raise
            raise StartupUnavailable(str(exc)) from exc

    def _put(self, value):
        while not self.stop.is_set():
            try:
                self.incoming.put(value, timeout=0.1)
                return
            except queue.Full:
                pass

    def _read(self):
        try:
            for raw in iter(self.p.stdout.readline, b""):
                self._put(raw)
                if self.stop.is_set():
                    break
        finally:
            self._put(None)

    def _write(self, gated):
        try:
            if gated:
                self.p.stdin.write(b"1"); self.p.stdin.flush()
            while not self.stop.is_set():
                try:
                    raw = self.outgoing.get(timeout=0.1)
                except queue.Empty:
                    continue
                self.p.stdin.write(raw); self.p.stdin.flush()
        except (OSError, ValueError):
            self._put(None)

    def send(self, message):
        self.outgoing.put((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))

    def _record(self, event):
        self.trace.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.trace.flush()

    def _receive(self, deadline, tick=None):
        while True:
            if self.cancel.is_set():
                raise Interrupted("App Server canceled; inspect the saved Thread before continuing.")
            if time.monotonic() >= deadline:
                raise Interrupted("App Server RPC/Turn timeout; inspect the saved Thread before continuing.")
            if tick and tick():
                return None
            try:
                raw = self.incoming.get(timeout=0.1)
            except queue.Empty:
                if self.p.poll() is not None:
                    raise WorkflowError("App Server exited before the requested result.")
                continue
            if raw is None:
                raise WorkflowError("App Server transport closed before the requested result.")
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise WorkflowError("Invalid App Server JSON-RPC object.")
            if "method" in event and "id" in event:
                self._server_request(event)
                continue
            if "method" in event:
                # Do not persist account/auth notifications or discovery replies.
                if not event["method"].startswith("account/"):
                    self._record(event)
                self.notify(event)
                return None
            return event

    def _server_request(self, event):
        method = event["method"]
        response = {"id": event["id"]}
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            response["result"] = {"decision": "decline"}
        else:
            response["error"] = {"code": -32601, "message": "Unsupported server request"}
        self._record({"server_request": method, "response": response})
        self.send(response)

    def call(self, method, params, timeout=None):
        self.seq += 1
        rid = self.seq
        self.send({"id": rid, "method": method, "params": params})
        deadline = min(self.deadline, time.monotonic() + timeout) if timeout else self.deadline
        while True:
            event = self._receive(deadline)
            if event is None:
                continue
            if event.get("id") != rid:
                raise WorkflowError("Unexpected App Server response ID.")
            if "error" in event:
                raise RpcError(method, event["error"])
            if "result" not in event:
                raise WorkflowError("RPC response is missing result.")
            return event["result"]

    def wait(self, predicate, tick=None):
        def poll():
            if tick:
                tick()
            return predicate()
        while not predicate():
            event = self._receive(self.deadline, poll if tick else None)
            if event is not None:
                raise WorkflowError("Unexpected asynchronous RPC response.")

    def close(self):
        self.stop.set()
        try:
            if self.p:
                stop_process(self.p)
                for t in (self.reader, self.writer):
                    if t:
                        t.join(timeout=2)
                for stream in (self.p.stdin, self.p.stdout):
                    if stream:
                        stream.close()
                if self.on_pid:
                    self.on_pid(self.p.pid, False)
        finally:
            if self.trace:
                self.trace.close()
            if self.err:
                self.err.close()

    def __exit__(self, *_):
        self.close()


def final_text(turn, fallback=None):
    texts = [x.get("text") for x in turn.get("items", []) or []
             if x.get("type") == "agentMessage" and x.get("phase") == "final_answer"
             and isinstance(x.get("text"), str)]
    if texts:
        return texts[-1]
    # Fallback to any agentMessage text if phase is omitted by a provider/version.
    any_texts = [x.get("text") for x in turn.get("items", []) or []
                 if x.get("type") == "agentMessage" and isinstance(x.get("text"), str)]
    return any_texts[-1] if any_texts else fallback


def _turn_sandbox_policy(sandbox):
    if sandbox == "read-only":
        return {"type": "readOnly", "networkAccess": False}
    if sandbox == "danger-full-access":
        return {"type": "dangerFullAccess"}
    return None


def _default_supplement_prompt(queued, has_schema):
    suffix = " Return the requested JSON output schema for the complete current result." if has_schema else ""
    return (
        "Apply these queued user clarifications in the same task."
        + suffix + "\n"
        + json.dumps([{"id": m["id"], "text": m["text"]} for m in queued], ensure_ascii=False)
    )


def run(ws, state, save_fn, launcher, requested, artifact_dir, inbox_target,
        prompt, on_line, *, sandbox="workspace-write", output_schema=None,
        timeout=900, cancel=None, supplement_prompt_builder=None,
        verify_external_thread=False):
    from routing import verify_provider
    from backends.usage import RoundUsage
    import inbox

    usage = RoundUsage(artifact_dir)

    def on_pid(pid, started):
        if started:
            state["child_pid"] = pid
            save_fn("process_started")
        else:
            state["last_child_pid"] = pid
            state["child_pid"] = None
            save_fn()

    with Client(ws.root_path, launcher, artifact_dir, requested=requested,
                timeout=timeout, cancel=cancel, on_pid=on_pid) as rpc:
        atomic_json(artifact_dir / "invocation.json", {
            "backend": "app-server", "argv": rpc.argv, "cwd": str(ws.root_path),
            "sandbox": sandbox, "started_at": now(), "codex_requested": requested,
        })
        existing_tid = state.get("thread_id") or state.get("requested_external_thread_id")
        if existing_tid and verify_external_thread:
            meta = rpc.call("thread/read", {"threadId": existing_tid, "includeTurns": False}, timeout=20).get("thread") or {}
            if meta.get("id") != existing_tid:
                raise WorkflowError("External Thread ID was not found or returned a mismatched Thread.")
            orig_cwd = meta.get("cwd")
            if not runtime.thread_cwd_matches_workspace(ws, orig_cwd):
                raise WorkflowError(
                    f"External Thread belongs to a different workspace ({orig_cwd}); refusing cross-workspace resume."
                )
            meta_status = (meta.get("status") or {}).get("type")
            if meta_status not in ("idle", "notLoaded"):
                raise WorkflowError(f"External Thread is not idle (status={meta_status}); refusing resume.")
            meta_provider = meta.get("modelProvider")
            if meta_provider:
                if requested.get("model_provider") not in (None, meta_provider):
                    raise WorkflowError("External Thread provider differs from requested provider.")
                verify_provider(ws.state_dir, meta_provider)

        params = {"cwd": str(ws.root_path), "sandbox": sandbox, "approvalPolicy": "never"}
        if requested.get("model"):
            params["model"] = requested["model"]
        if requested.get("model_provider"):
            params["modelProvider"] = requested["model_provider"]
        method = "thread/resume" if existing_tid else "thread/start"
        if existing_tid:
            params["threadId"] = existing_tid
            params["excludeTurns"] = True
        result = rpc.call(method, params, timeout=30)
        thread = result.get("thread") or {}
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise WorkflowError("App Server returned no Thread ID.")
        if existing_tid and existing_tid != thread_id:
            raise WorkflowError("Resumed App Server returned a different Session ID.")
        if existing_tid and verify_external_thread and thread.get("cwd"):
            if not runtime.thread_cwd_matches_workspace(ws, thread.get("cwd")):
                raise WorkflowError("Resumed external Thread cwd does not match this workspace.")
        if existing_tid and (thread.get("status") or {}).get("type") != "idle":
            idle = rpc.call("thread/read", {"threadId": thread_id, "includeTurns": False}).get("thread") or {}
            if (idle.get("status") or {}).get("type") != "idle":
                raise WorkflowError("Resumed Thread is not confirmed idle.")
        provider = result.get("modelProvider")
        if requested.get("model_provider") and provider != requested["model_provider"]:
            raise WorkflowError("App Server Thread provider differs from requested provider.")
        verify_provider(ws.state_dir, provider)
        state.update(
            backend="app-server", model_provider=provider,
            backend_model=result.get("model"), backend_effort=result.get("reasoningEffort"),
            active_turn_id=None,
        )
        completed, texts = {}, {}
        with (artifact_dir / "codex.jsonl").open("wb") as stream:
            def emit(event):
                raw = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
                stream.write(raw); stream.flush(); on_line(raw)

            emit({"type": "thread.started", "thread_id": thread_id})

            def notify(event):
                m, args = event.get("method"), event.get("params") or {}
                if args.get("threadId") != thread_id:
                    return
                if m in ("turn/started", "turn/completed"):
                    turn = args.get("turn") or {}
                    tid = turn.get("id")
                    if not tid:
                        return
                    if m == "turn/started" and state.get("turn_pending"):
                        if state.get("active_turn_id") not in (None, tid):
                            raise WorkflowError("Unexpected concurrent Turn in owned Thread.")
                        state["active_turn_id"] = tid
                        save_fn("turn_bound")
                    if m == "turn/completed":
                        completed[tid] = turn
                elif m == "thread/tokenUsage/updated" and state.get("turn_pending"):
                    # Only the bound current Turn may establish or advance this round's usage baseline.
                    active = state.get("active_turn_id")
                    if active and args.get("turnId") == active:
                        usage.update(args)
                elif m in ("item/started", "item/completed"):
                    item = args.get("item") or {}
                    typ = item.get("type")
                    if typ == "agentMessage" and item.get("phase") == "final_answer":
                        texts[args.get("turnId")] = item.get("text")
                    elif typ in ("commandExecution", "fileChange"):
                        emit({"type": "item." + m.split("/")[1], "item": {
                            "type": "command_execution" if typ == "commandExecution" else "file_change",
                            "command": item.get("command"), "exit_code": item.get("exitCode"),
                            "status": item.get("status"), "changes": item.get("changes", []),
                        }})

            rpc.notify = notify
            queued = []
            next_prompt = prompt
            while True:
                window = inbox.open_window(inbox_target, state)
                accepted = []
                interrupt_requested = False
                tid = None
                turn = None
                seen = 0
                turn_params = {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": next_prompt}],
                }
                if output_schema is not None:
                    turn_params["outputSchema"] = output_schema
                turn_policy = _turn_sandbox_policy(sandbox)
                if turn_policy is not None:
                    turn_params["sandboxPolicy"] = turn_policy
                if requested.get("model"):
                    turn_params["model"] = requested["model"]
                if requested.get("reasoning_effort"):
                    turn_params["effort"] = requested["reasoning_effort"]
                state.update(turn_pending=True, active_turn_id=None)
                save_fn("turn_submitting")
                try:
                    started = rpc.call("turn/start", turn_params)
                    tid = (started.get("turn") or {}).get("id")
                    if not isinstance(tid, str) or not tid or state.get("active_turn_id") not in (None, tid):
                        raise WorkflowError("App Server Turn identity could not be reconciled.")
                    state["active_turn_id"] = tid
                    save_fn("turn_bound")
                    for message in queued:
                        accepted.append(inbox.ack(inbox_target, message, "rpc_accepted",
                                                  turn_id=tid, delivery_action="queue"))

                    def tick():
                        nonlocal interrupt_requested, seen
                        if tid in completed or interrupt_requested:
                            return
                        for path, candidate in inbox.records(window, "pending"):
                            if tid in completed or interrupt_requested:
                                break
                            action = candidate.get("delivery_action", candidate["action"])
                            if action == "queue":
                                continue
                            message = inbox.claim(window, path)
                            if message is None:
                                continue
                            seen += 1
                            if seen > inbox.MAX_MESSAGES_PER_WINDOW and action != "interrupt":
                                inbox.ack(inbox_target, message, "rejected", reason="Live message budget reached.")
                                continue
                            if action == "steer":
                                try:
                                    answer = rpc.call("turn/steer", {
                                        "threadId": thread_id,
                                        "expectedTurnId": tid,
                                        "input": [{"type": "text", "text": message["text"]}],
                                    })
                                except RpcError as exc:
                                    if exc.code == -32600 and "no active turn to steer" in exc.message.lower():
                                        inbox.requeue(window, message)
                                        continue
                                    inbox.ack(inbox_target, message, "rejected", reason=str(exc))
                                    continue
                                if answer.get("turnId") != tid:
                                    raise WorkflowError("Steer response has unexpected Turn ID.")
                                accepted.append(inbox.ack(inbox_target, message, "rpc_accepted",
                                                          turn_id=tid, delivery_action="steer"))
                            elif action == "interrupt":
                                interrupt_requested = True
                                try:
                                    rpc.call("turn/interrupt", {"threadId": thread_id, "turnId": tid})
                                except RpcError as exc:
                                    stale = (exc.code == -32600 and exc.message.strip().lower().rstrip(".")
                                             in ("no active turn", "no active turn to interrupt"))
                                    if not stale:
                                        raise
                                    inbox.ack(inbox_target, message, "deferred",
                                              reason="Turn ended before interrupt took effect; pause before further work.")
                                else:
                                    accepted.append(inbox.ack(inbox_target, message, "rpc_accepted",
                                                              turn_id=tid, delivery_action="interrupt"))

                    rpc.wait(lambda: tid in completed, tick=tick)
                    turn = completed[tid]
                    status = turn.get("status")
                    for message in accepted:
                        inbox.ack(inbox_target, message, {
                            "completed": "turn_completed",
                            "interrupted": "turn_interrupted",
                        }.get(status, "turn_failed"), turn_id=tid)
                    allow_queue = (status == "completed" and not interrupt_requested
                                   and state.get("queue_turns", 0) < inbox.MAX_QUEUE_TURNS)
                    queued, late_interrupt = inbox.drain(
                        inbox_target, state, window, allow_queue=allow_queue,
                        reason="Turn interrupted/failed or supplement Turn budget reached; submit a new task if needed.",
                    )
                    interrupt_requested = interrupt_requested or late_interrupt
                except BaseException:
                    inbox.drain(inbox_target, state, window, allow_queue=False,
                                reason="Turn outcome needs reconciliation; message was not submitted.")
                    raise

                if status == "completed":
                    text = final_text(turn, texts.get(tid))
                    if isinstance(text, str):
                        (artifact_dir / "last-message.md").write_text(text, encoding="utf-8")
                    elif (artifact_dir / "last-message.md").exists():
                        (artifact_dir / "last-message.md").unlink()
                else:
                    emit({"type": "turn.failed", "error": turn.get("error") or status})

                if status == "completed" and not queued:
                    emit({"type": "turn.completed", "usage": usage.data})
                    atomic_json(artifact_dir / "summary.json", {
                        "session_id": thread_id, "exit_code": 0,
                        "completed": True, "failed": False, "invalid_lines": 0, "usage": usage.data,
                    })
                state.update(last_turn_id=tid, last_turn_status=status,
                             turn_pending=False, active_turn_id=None)
                save_fn("turn_terminal")
                if status == "interrupted":
                    raise Interrupted("App Server Turn interrupted; continue the same Session.")
                if status != "completed":
                    return 1
                if queued:
                    state["queue_turns"] = state.get("queue_turns", 0) + 1
                    save_fn("supplement_turn_reserved")
                    if supplement_prompt_builder:
                        next_prompt = supplement_prompt_builder(queued)
                    else:
                        next_prompt = _default_supplement_prompt(queued, output_schema is not None)
                    continue
                if interrupt_requested:
                    raise Interrupted("Interrupt arrived as Turn completed; task paused before further steps.")
                return 0


def _find_turn_paginated(rpc, thread_id, active_turn, fallback_turns=None, *, max_pages=50):
    """Page through thread/turns/list using nextCursor; stop immediately when active_turn is found."""
    cursor = None
    seen = set()
    for _ in range(max_pages):
        params = {"threadId": thread_id, "itemsView": "full"}
        if cursor is not None:
            params["cursor"] = cursor
        page = rpc.call("thread/turns/list", params)
        turns = page.get("data") or []
        turn = next((t for t in turns if t.get("id") == active_turn), None)
        if turn is not None:
            return turn
        cursor = page.get("nextCursor")
        if not cursor:
            break
        if cursor in seen:
            raise WorkflowError("App Server thread/turns/list pagination did not terminate.")
        seen.add(cursor)
    return next((t for t in (fallback_turns or []) if t.get("id") == active_turn), None)


def reconcile_run(ws, state, save_fn, artifact_dir, inbox_target, *,
                  codex_cmd=None, timeout=30, cancel=None):
    """Read-only recovery for an ambiguous Turn; never blindly replays."""
    if not state.get("turn_pending"):
        return
    thread_id = state.get("thread_id")
    active_turn = state.get("active_turn_id")
    if not thread_id or not active_turn:
        raise WorkflowError("Submission has no durable Turn ID; inspect native Thread history manually.")
    launcher = codex_cmd or resolve_codex()
    with Client(ws.root_path, launcher, artifact_dir / "recovery",
                requested={"model_provider": state.get("model_provider")},
                timeout=timeout, cancel=cancel) as rpc:
        thread = rpc.call("thread/read", {"threadId": thread_id, "includeTurns": False}).get("thread") or {}
        if thread.get("id") == thread_id and (thread.get("status") or {}).get("type") == "notLoaded":
            params = {
                "threadId": thread_id, "cwd": str(ws.root_path),
                "sandbox": state.get("sandbox", "workspace-write"),
                "approvalPolicy": "never", "excludeTurns": True,
            }
            if state.get("model_provider"):
                params["modelProvider"] = state["model_provider"]
            resumed = rpc.call("thread/resume", params)
            if (resumed.get("thread") or {}).get("id") != thread_id:
                raise WorkflowError("Recovery loaded a different Thread.")
            if state.get("model_provider") and resumed.get("modelProvider") != state["model_provider"]:
                raise WorkflowError("Recovery loaded a different provider.")
            thread = rpc.call("thread/read", {"threadId": thread_id, "includeTurns": False}).get("thread") or {}
        if thread.get("id") != thread_id or (thread.get("status") or {}).get("type") != "idle":
            raise WorkflowError("Saved App Server Thread is not confirmed idle.")
        turn = _find_turn_paginated(rpc, thread_id, active_turn, thread.get("turns"))
        if not turn or turn.get("status") not in ("completed", "failed", "interrupted"):
            raise WorkflowError("Saved Turn has no confirmed terminal outcome.")
        if turn["status"] == "completed":
            text = final_text(turn)
            if text is None:
                raise WorkflowError("Completed Turn has no recoverable final answer; inspect evidence.")
            (artifact_dir / "last-message.md").write_text(text, encoding="utf-8")
            from backends.usage import observed
            obs = observed(artifact_dir)
            state["usage"] = obs
            atomic_json(artifact_dir / "summary.json", {
                "session_id": thread_id, "exit_code": 0,
                "completed": True, "failed": False, "invalid_lines": 0, "usage": obs,
            })
        import inbox
        inbox.finish_accepted(inbox_target, active_turn, turn["status"])
        state.update(
            turn_pending=False, last_turn_id=active_turn,
            last_turn_status=turn["status"], active_turn_id=None,
        )
        save_fn("turn_reconciled")


def probe_windows_sandbox_readiness(cwd, launcher=None, timeout=15):
    """Best-effort read-only probe of Codex's native windowsSandbox/readiness RPC."""
    if os.name != "nt":
        return None
    import tempfile
    try:
        cmd = launcher or resolve_codex()
        with tempfile.TemporaryDirectory(prefix="agy-winsandbox-") as directory:
            with Client(cwd, cmd, directory, timeout=timeout) as rpc:
                resp = rpc.call("windowsSandbox/readiness", {}, timeout=10)
                return resp.get("status") if isinstance(resp, dict) else None
    except Exception:
        return None
