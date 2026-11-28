#!/usr/bin/env python3
"""Strict engineering Ticket controller consuming the shared Codex runtime."""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

_scripts_dir = str(Path(__file__).resolve().parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)
if __name__ == "__main__":
    sys.modules.setdefault("controller", sys.modules[__name__])

import runtime
from runtime import (
    EventSummary,
    Interrupted,
    VERSION,
    WindowsJob,
    WorkflowError,
    Workspace,
    _metadata_io,
    atomic_json,
    codex_exec_flags,
    codex_settings,
    configured_codex,
    current_codex_home,
    encode,
    git,
    launch_gate,
    norm_path_key,
    now,
    pid_alive,
    read_json,
    resolve_codex,
    stop_process,
    worktree_lock,
)

TERMINAL = {"PASS", "ESCALATE"}
ISSUE_FIELDS = ("kind", "detail", "evidence", "question", "recommendation")
WORKER_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["outcome", "summary", "changes", "checks", "issue"],
    "properties": {
        "outcome": {"type": "string", "enum": ["done", "decision_required", "blocked"]},
        "summary": {"type": "string"},
        "changes": {"type": "array", "items": {"type": "string"}},
        "checks": {"type": "array", "items": {"type": "string"}},
        "issue": {"anyOf": [{"type": "null"}, {
            "type": "object", "additionalProperties": False,
            "required": list(ISSUE_FIELDS),
            "properties": {**{k: {"type": "string"} for k in ISSUE_FIELDS},
                           "kind": {"type": "string", "enum": ["engineering_choice", "product_decision", "environment", "permission"]}},
        }]},
    },
}
REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["ticket_id", "round", "snapshot_sha256", "verdict", "summary", "axes", "findings", "blocker"],
    "properties": {
        "ticket_id": {"type": "string"}, "round": {"type": "integer"},
        "snapshot_sha256": {"type": "string"},
        "verdict": {"type": "string", "enum": ["PASS", "REVISE", "ESCALATE"]},
        "summary": {"type": "string"},
        "axes": {"type": "object", "additionalProperties": False,
                 "required": ["spec", "standards"],
                 "properties": {"spec": {"type": "string"}, "standards": {"type": "string"}}},
        "findings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "path", "reason", "requested_fix"],
            "properties": {k: {"type": "string"} for k in ("id", "path", "reason", "requested_fix")},
        }},
        "blocker": {"anyOf": [{"type": "null"}, {
            "type": "object", "additionalProperties": False,
            "required": list(ISSUE_FIELDS),
            "properties": {**{k: {"type": "string"} for k in ISSUE_FIELDS},
                           "kind": {"type": "string", "enum": ["product_decision", "environment", "permission"]}},
        }]},
    },
}


def validate_spec(spec):
    """Validate a frozen task without creating state or starting a process."""
    for field in ("title", "goal"):
        if not isinstance(spec.get(field), str) or not spec[field].strip():
            raise WorkflowError(f"Ticket requires a nonempty {field}.")
    if not isinstance(spec.get("acceptance"), list) or not spec["acceptance"] or not all(isinstance(x, str) and x.strip() for x in spec["acceptance"]):
        raise WorkflowError("Ticket requires an acceptance list of nonempty strings.")
    commands = spec.get("test_commands")
    if not isinstance(commands, list) or not commands or not all(isinstance(c, list) and c and all(isinstance(x, str) and x for x in c) for c in commands):
        raise WorkflowError("test_commands must contain argv arrays, for example [['{python}', '-m', 'unittest']].")
    scope = spec.get("allowed_paths")
    if scope is not None and (not isinstance(scope, list) or not scope or not all(
            isinstance(x, str) and x and not x.startswith(('/', '\\')) and ':' not in x
            and '..' not in PurePosixPath(x.replace('\\', '/')).parts
            and PurePosixPath(x.replace('\\', '/')).parts[:1] != ('.git',)
            for x in scope)):
        raise WorkflowError("allowed_paths must contain repository-relative files, directory prefixes, or globs outside Git metadata.")
    codex_settings(spec.get("codex"))
    limit = spec.get("max_rounds", 3)
    if type(limit) is not int or not 1 <= limit <= 3:
        raise WorkflowError("max_rounds must be an integer from 1 through 3.")
    return limit


class Controller:
    def __init__(self, repo, codex_cmd=None, timeout=900):
        candidate = Path(repo).resolve()
        self.repo = Path(git(candidate, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        self.gitdir = Path(git(self.repo, "rev-parse", "--absolute-git-dir").decode().strip()).resolve()
        self.root = self.gitdir / "superagent"
        self.lock = self.root / "worktree.lock"
        self.codex_cmd = codex_cmd
        self.timeout = timeout
        self.cancel = threading.Event()
        self.progress_callback = None

    @property
    def workspace(self):
        key = "git:" + hashlib.sha256(norm_path_key(self.gitdir).encode("utf-8")).hexdigest()[:16]
        return Workspace(
            cwd=self.repo, root_path=self.repo, git_dir=self.gitdir,
            state_dir=self.root, lock_path=self.lock,
            workspace_key=key, is_git=True,
        )

    def _progress(self, kind, **fields):
        if self.progress_callback is not None:
            self.progress_callback({"kind": kind, **fields})

    def ticket_dir(self, ticket_id):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", ticket_id):
            raise WorkflowError("Ticket ID must contain only letters, digits, underscore, hyphen (1-64 characters).")
        return self.root / "tickets" / ticket_id

    def load(self, ticket_id):
        state = read_json(self.ticket_dir(ticket_id) / "ticket.json")
        if Path(state["worktree"]).resolve() != self.repo or Path(state["git_dir"]).resolve() != self.gitdir:
            raise WorkflowError("Ticket belongs to a different worktree. Do not move its state directory.")
        return state

    def save(self, state, event=None):
        state["updated_at"] = now()
        directory = self.ticket_dir(state["id"])
        atomic_json(directory / "ticket.json", state)
        if event:
            with (directory / "events.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"at": now(), "event": event, "status": state["status"],
                                    "round": state["round"], "session_id": state["session_id"]}, ensure_ascii=False) + "\n")
                f.flush(); os.fsync(f.fileno())

    def _active_snapshot_paths(self):
        active = self.root / "active.json"
        return self.load(read_json(active)["ticket_id"]).get("snapshot_paths", []) if active.exists() else []

    def _explicit_file_paths(self, scope):
        """Include named files, including future files, without walking ignored trees."""
        paths = set()
        for raw in scope or []:
            name = raw.replace("\\", "/")
            if name.endswith("/") or any(c in name for c in "*?["):
                continue
            name = PurePosixPath(name).as_posix()
            path = self.repo / name
            if name != "." and (path.is_symlink() or not path.is_dir()):
                paths.add(name)
        return paths

    def snapshot_for(self, state):
        if "snapshot_paths" not in state:
            candidates = self._explicit_file_paths(state["spec"].get("allowed_paths"))
            if candidates and git(self.repo, "check-ignore", "--", *sorted(candidates), allowed=(0, 1)):
                raise WorkflowError("This legacy Ticket did not capture explicitly allowed ignored files. "
                                    "Close it and start a new scoped Ticket to record their baseline before continuing.")
        return self.snapshot(state.get("snapshot_paths", []))

    def snapshot(self, paths=None):
        head = git(self.repo, "rev-parse", "HEAD").decode().strip()
        index = git(self.repo, "ls-files", "--stage", "-z")
        if any(row.startswith(b"160000 ") for row in index.split(b"\0") if row):
            raise WorkflowError("This MVP requires a worktree without submodules; split a submodule into its own Ticket.")
        names = set(git(self.repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0")) - {b""}
        for name in self._active_snapshot_paths() if paths is None else paths:
            path = self.repo / name
            parent = path.parent.resolve()
            if not parent.is_relative_to(self.repo) or parent.is_relative_to(self.gitdir):
                raise WorkflowError("Explicit task path must stay inside the worktree and outside Git metadata: " + name)
            if path.is_symlink() or path.is_file():
                names.add(os.fsencode(name))
        files = {}
        for raw in sorted(names):
            name = os.fsdecode(raw)
            path = self.repo / name
            if path.is_symlink():
                files[name] = {"kind": "symlink", "target": os.readlink(path)}
            elif path.is_file():
                digest = hashlib.sha256()
                with path.open("rb") as f:
                    for block in iter(lambda: f.read(1024 * 1024), b""):
                        digest.update(block)
                files[name] = {"sha256": digest.hexdigest(), "executable": bool(path.stat().st_mode & 0o111)}
            elif not path.exists():
                files[name] = {"kind": "missing"}
            else:
                raise WorkflowError(f"Unsupported tracked file type: {name}")
        value = {"head": head, "index_sha256": hashlib.sha256(index).hexdigest(), "files": files}
        value["sha256"] = hashlib.sha256(encode(value).encode()).hexdigest()
        return value

    def _active(self, state=None):
        path = self.root / "active.json"
        if path.exists():
            active = self.load(read_json(path)["ticket_id"])
            if state and active["id"] == state["id"]:
                return
            if active["status"] not in TERMINAL or active.get("resolution_allowed") or active.get("recoverable_interruption"):
                raise WorkflowError(f"Worktree is reserved by {active['id']} ({active['status']}). Continue it or explicitly close it first.")
            if pid_alive(active.get("child_pid"), process_group=os.name != "nt"):
                raise WorkflowError("Previous worker or its process group is still running.")
        if state:
            atomic_json(path, {"ticket_id": state["id"]})

    def create(self, spec, *, expected_sha256=None):
        spec = dict(spec)
        ticket_id = spec.get("id", "")
        directory = self.ticket_dir(ticket_id)
        limit = validate_spec(spec)
        worker_config = codex_settings(configured_codex(self.root), spec.get("codex"))
        scope = spec.get("allowed_paths")
        with worktree_lock(self.lock):
            self._active()
            if directory.exists():
                raise WorkflowError("Ticket already exists; use status/run/recover instead of replacing it.")
            previous_paths = self._active_snapshot_paths()
            snapshot_paths = sorted(set(previous_paths) | self._explicit_file_paths(scope))
            snap = self.snapshot(snapshot_paths)
            if expected_sha256 is not None:
                if snapshot_paths == previous_paths:
                    prev_sha = snap["sha256"]
                else:
                    # Project previous_paths view from snap in memory instead of re-hashing the whole repo.
                    added_explicit = set(snapshot_paths) - set(previous_paths)
                    if added_explicit:
                        payload = b"\0".join(os.fsencode(x) for x in sorted(added_explicit)) + b"\0"
                        ignored_raw = git(self.repo, "check-ignore", "--stdin", "-z",
                                          allowed=(0, 1), input_bytes=payload)
                        ignored_added = {os.fsdecode(x) for x in ignored_raw.split(b"\0") if x}
                    else:
                        ignored_added = set()
                    prev_files = {k: v for k, v in snap["files"].items() if k not in ignored_added}
                    prev_value = {"head": snap["head"], "index_sha256": snap["index_sha256"], "files": prev_files}
                    prev_sha = hashlib.sha256(encode(prev_value).encode()).hexdigest()
                if prev_sha != expected_sha256:
                    raise WorkflowError("Worktree changed between plan steps; inspect the change before starting the next task.")
            state = {"schema_version": 2, "baseline_format": "files-v1", "id": ticket_id, "spec": spec, "status": "READY", "round": 0,
                     "snapshot_paths": snapshot_paths,
                     "max_rounds": limit, "codex_requested": worker_config, "session_id": None, "worktree": str(self.repo), "git_dir": str(self.gitdir),
                     "codex_home": current_codex_home(),
                     "base_commit": snap["head"], "expected_sha256": snap["sha256"], "created_at": now(),
                     "host": socket.gethostname(), "runner_pid": None, "child_pid": None, "last_review": None,
                     "reason": None, "resolutions": [], "resolution_allowed": False}
            directory.mkdir(parents=True)
            try:
                scoped_names = [n for n in snap["files"] if self._in_scope(n, scope)]
                # _capture_files verifies each copied file's SHA-256 against snap under worktree_lock.
                self._capture_files(snap, directory / "baseline-files", scoped_names)
                atomic_json(directory / "baseline.json", snap)
                atomic_json(directory / "contract.json", spec)
            except Exception:
                shutil.rmtree(directory)  # Only our newly created private evidence, never user source.
                raise
            self.save(state, "created")
            self._active(state)
            return state

    def _round_dir(self, state):
        return self.ticket_dir(state["id"]) / "rounds" / f"{state['round']:02d}"

    def _contract_path(self, state):
        directory = self.ticket_dir(state["id"])
        return directory / ("contract.json" if (directory / "contract.json").exists() else "ticket.json")

    def _prompt(self, state):
        header = (f"Ticket {state['id']}; implementation {state['round']}/{state['max_rounds']}.\n"
                  f"Assigned worktree: {self.repo}\n")
        reference = Path(__file__).resolve().parent.parent / "references" / "execution.md"
        if not state["session_id"]:
            rules = reference.read_text(encoding="utf-8")
            spec = {k: v for k, v in state["spec"].items() if k != "request_fingerprint"}
            return header + rules.rstrip() + "\n\nINITIAL_TASK\n" + encode(spec)
        previous = state.get("last_review") or {}
        feedback = {"summary": previous.get("summary") or state.get("reason"),
                    "unresolved_findings": previous.get("findings", [])}
        return (header + "Continue in this same Codex Session. Scope and acceptance are unchanged. "
                "Apply the unresolved findings below; inspect current files first after an interruption. "
                "Make routine reversible engineering choices within the contract; preserve pre-existing edits. "
                "Do not restart planning, delegate editing, invoke AGY Codex/code-review, weaken tests, "
                "edit controller state, or stage/commit/push/merge/rebase/reset/clean/switch Git state. "
                "AGY owns the read-only review.\n"
                f"If context is missing, read the saved contract at {self._contract_path(state)} "
                f"and execution rules at {reference}. Follow their plan pointers only as needed.\n"
                "REPAIR_DELTA\n" + encode(feedback) +
                "Return the supplied JSON output schema. Report done, decision_required, or blocked; "
                "include observed evidence for a real blocker. The Router handles user communication.\n")

    def _process(self, state, argv, prompt, stdout_path, stderr_path, on_line=None, timeout=None):
        def on_pid(pid, started):
            if started:
                state["child_pid"] = pid
                self.save(state, "process_started")
            else:
                state["last_child_pid"] = pid
                state["child_pid"] = None
                self.save(state)

        return runtime.run_process(
            self.repo, argv, prompt, stdout_path, stderr_path,
            on_line=on_line, on_pid=on_pid,
            timeout=timeout or self.timeout, cancel=self.cancel,
        )

    def _escalate(self, state, reason, recoverable=False):
        state.update(status="ESCALATE", reason=reason, runner_pid=None,
                     recoverable_interruption=recoverable, resolution_allowed=False, stop_origin="controller")
        self.save(state, "escalated")
        return state

    def _sync_from_run(self, state, run_state, event=None):
        """Project single-source Run execution facts onto the owning Ticket record."""
        state["run_id"] = run_state["run_id"]
        if run_state.get("thread_id"):
            state["session_id"] = run_state["thread_id"]
        if run_state.get("backend"):
            state["backend"] = run_state["backend"]
        if run_state.get("model_provider"):
            state["model_provider"] = run_state["model_provider"]
        if "backend_model" in run_state:
            state["backend_model"] = run_state.get("backend_model")
        if "backend_effort" in run_state:
            state["backend_effort"] = run_state.get("backend_effort")
        state["app_server_pending"] = bool(run_state.get("turn_pending"))
        state["active_turn_id"] = run_state.get("active_turn_id")
        state["last_turn_id"] = run_state.get("last_turn_id")
        state["last_turn_status"] = run_state.get("last_turn_status")
        state["inbox_window"] = run_state.get("inbox_window")
        state["queue_turns"] = run_state.get("queue_turns", state.get("queue_turns", 0))
        state["child_pid"] = run_state.get("child_pid")
        if run_state.get("last_child_pid") is not None:
            state["last_child_pid"] = run_state.get("last_child_pid")
        self.save(state, event)

    def run(self, ticket_id, *, codex=None):
        with worktree_lock(self.lock):
            state = self.load(ticket_id)
            self._active(state)
            if state["status"] not in {"READY", "REVISE"}:
                raise WorkflowError(f"Cannot run from {state['status']}; use status, review, or recover.")
            if state["round"] >= state["max_rounds"]:
                return self._escalate(state, "Implementation round limit reached; no fourth worker call.")
            if state["host"] != socket.gethostname() or state["codex_home"] != current_codex_home():
                return self._escalate(state, "Host or CODEX_HOME changed; session provenance must be reconciled.")
            if self.snapshot_for(state)["sha256"] != state["expected_sha256"]:
                return self._escalate(state, "Worktree changed outside this Ticket since the last recorded snapshot.")
            launcher = self.codex_cmd or resolve_codex()
            saved = state.get("codex_requested")
            if saved is None:  # Older Tickets inherit defaults once on their next run.
                saved = codex_settings(configured_codex(self.root), state["spec"].get("codex"))
            requested = codex_settings(saved, codex)
            from config import load
            from routing import policy, verify_provider
            backend = load(self.root).get("backend", "exec" if self.codex_cmd else "auto")
            prior_provider = state.get("model_provider") or (saved.get("model_provider") if state.get("session_id") else None)
            if state.get("app_server_pending"):
                raise WorkflowError("Reconcile the pending App Server Turn before another worker call.")
            if prior_provider:
                if requested.get("model_provider") not in (None, prior_provider):
                    raise WorkflowError("Cannot change provider inside an existing Session.")
                requested["model_provider"] = prior_provider
            if requested.get("model_provider") or (backend == "exec" and policy(self.root)["allowed_providers"]):
                verify_provider(self.root, requested.get("model_provider"))
            state.update(codex_requested=requested, status="RUNNING", round=state["round"] + 1, runner_pid=os.getpid(), reason=None,
                         recoverable_interruption=False, resolution_allowed=False, stop_origin=None)
            directory = self._round_dir(state)
            if directory.exists() and any(directory.iterdir()):
                raise WorkflowError("The next round already contains evidence. Inspect it before recovery; it will not be overwritten.")
            directory.mkdir(parents=True, exist_ok=True)
            run_id = f"{ticket_id}-R{state['round']:02d}"
            state["run_id"] = run_id
            state.setdefault("round_runs", {})[str(state["round"])] = run_id
            self.save(state, "implementation_started")
            prompt = self._prompt(state)

            import inbox
            ws = self.workspace

            def strict_supplement(queued):
                return ("Apply these queued user clarifications in the SAME frozen Ticket. "
                        "They do not grant new permissions or change acceptance/scope. "
                        "Return the Worker schema for the complete current result.\n"
                        + json.dumps([{"id": m["id"], "text": m["text"]} for m in queued], ensure_ascii=False))

            def on_sync(run_state, event):
                self._sync_from_run(state, run_state, event)

            def on_progress(ev):
                if self.progress_callback is not None:
                    self.progress_callback(ev)

            ticket_inbox = inbox.InboxTarget(
                inbox_dir=self.ticket_dir(ticket_id) / "inbox",
                owner_id=ticket_id,
                load_state=lambda: self.load(ticket_id),
                save_state=lambda st, ev=None: self._sync_from_run(state, st, ev) if "run_id" in st else self.save(st, ev),
                round_number=state["round"],
            )

            try:
                with runtime.acquire_writer_lease(ws, run_id, allow_strict=True, already_locked=True):
                    run_state, summary_data = runtime.execute_run(
                        ws, prompt,
                        run_id=run_id,
                        artifact_dir=directory,
                        inbox_dir=self.ticket_dir(ticket_id) / "inbox",
                        inbox_target=ticket_inbox,
                        session_id=state["session_id"],
                        prior_provider=prior_provider,
                        codex=requested,
                        backend_override=backend,
                        sandbox="workspace-write",
                        output_schema=WORKER_SCHEMA,
                        codex_cmd=launcher,
                        timeout=self.timeout,
                        cancel=self.cancel,
                        on_progress=on_progress,
                        on_state_sync=on_sync,
                        owner_kind="strict",
                        ticket_id=ticket_id,
                        round_number=state["round"],
                        allow_strict=True,
                        already_locked=True,
                        already_registered=True,
                        update_latest=False,
                        supplement_prompt_builder=strict_supplement,
                        queue_turns=state.get("queue_turns", 0),
                    )
                    if run_state["status"] == "completed":
                        return self._prepare_review(state)
                completed_worker = (
                    summary_data.get("exit_code") == 0 and summary_data.get("completed")
                    and not summary_data.get("failed") and not summary_data.get("invalid_lines")
                    and summary_data.get("session_id") == state["session_id"]
                )
                recoverable = bool(state["session_id"]) and (
                    run_state["status"] in {"interrupted", "unknown"}
                    or completed_worker
                    or bool(state.get("app_server_pending"))
                )
                return self._escalate(
                    state,
                    run_state.get("error") or "Codex execution did not complete cleanly; inspect summary.json and stderr.log.",
                    recoverable=recoverable,
                )
            except (Exception, KeyboardInterrupt) as exc:
                saved_summary = directory / "summary.json"
                summary_data = read_json(saved_summary) if saved_summary.exists() else {}
                summary_data["controller_error"] = str(exc) or type(exc).__name__
                atomic_json(saved_summary, summary_data)
                completed_worker = (
                    summary_data.get("exit_code") == 0 and summary_data.get("completed")
                    and not summary_data.get("failed") and not summary_data.get("invalid_lines")
                    and summary_data.get("session_id") == state["session_id"]
                )
                recoverable = bool(state["session_id"]) and (
                    isinstance(exc, (Interrupted, KeyboardInterrupt))
                    or completed_worker
                    or bool(state.get("app_server_pending"))
                )
                return self._escalate(state, summary_data["controller_error"], recoverable=recoverable)

    def _changed(self, snapshot, baseline):
        return sorted(n for n in set(snapshot["files"]) | set(baseline["files"])
                      if snapshot["files"].get(n) != baseline["files"].get(n))

    @staticmethod
    def _in_scope(name, scope):
        if not scope:
            return True
        norm_name = PurePosixPath(name.replace("\\", "/")).as_posix()
        parts = norm_name.split("/")
        for raw in scope:
            p = raw.replace("\\", "/")
            directory = p.endswith("/")
            p = PurePosixPath(p).as_posix()
            if p == ".":
                return True
            if not any(c in p for c in "*?["):
                if norm_name.startswith(p + "/") or (not directory and norm_name == p):
                    return True
                continue
            patterns = p.split("/") + (["*", "**"] if directory else [])
            positions = {0}
            for pattern in patterns:
                if pattern == "**":
                    positions = set(range(min(positions), len(parts) + 1)) if positions else set()
                else:
                    positions = {i + 1 for i in positions
                                 if i < len(parts) and fnmatch.fnmatchcase(parts[i], pattern)}
            if len(parts) in positions:
                return True
        return False

    def _capture_files(self, snapshot, destination, names):
        """Freeze scoped bytes; symlinks are represented by target text, never followed."""
        for name in names:
            info = snapshot["files"].get(name, {"kind": "missing"})
            if info.get("kind") == "missing":
                continue
            source, target = self.repo / name, destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if info.get("kind") == "symlink":
                if not source.is_symlink() or os.readlink(source) != info["target"]:
                    raise WorkflowError("File changed during snapshot: " + name)
                target.write_bytes(os.fsencode(info["target"]))
            else:
                if source.is_symlink():
                    raise WorkflowError("File type changed during snapshot: " + name)
                shutil.copyfile(source, target)
                if hashlib.sha256(target.read_bytes()).hexdigest() != info["sha256"]:
                    raise WorkflowError("File changed during snapshot: " + name)
                target.chmod(0o755 if info["executable"] else 0o644)

    def _task_diff(self, state, baseline, snapshot, changed):
        directory = self._round_dir(state)
        if state.get("baseline_format") != "files-v1":
            patch = git(self.repo, "diff", "--no-ext-diff", "--no-textconv", "--binary", "--full-index", state["base_commit"], "--")
            for name in git(self.repo, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
                if name:
                    patch += git(self.repo, "diff", "--no-ext-diff", "--no-textconv", "--binary", "--no-index", "--", os.devnull, os.fsdecode(name), allowed=(0, 1))
            return patch
        before_dir = self.ticket_dir(state["id"]) / "baseline-files"
        after_dir = directory / "changed-files"
        self._capture_files(snapshot, after_dir, changed)
        patch = b""
        metadata = []
        for name in changed:
            old = baseline["files"].get(name, {"kind": "missing"})
            new = snapshot["files"].get(name, {"kind": "missing"})
            old_path = before_dir / name if old.get("kind") != "missing" else Path(os.devnull)
            new_path = after_dir / name if new.get("kind") != "missing" else Path(os.devnull)
            if old.get("sha256") and hashlib.sha256(old_path.read_bytes()).hexdigest() != old["sha256"]:
                raise WorkflowError("Saved baseline content changed: " + name)
            if old.get("kind") == "symlink" and old_path.read_bytes() != os.fsencode(old["target"]):
                raise WorkflowError("Saved baseline link changed: " + name)
            metadata.append({"path": name, "before": old, "after": new})
            patch += ("\n# TASK PATH " + json.dumps(name, ensure_ascii=False) + "\n").encode("utf-8")
            patch += git(self.repo, "diff", "--no-ext-diff", "--no-textconv", "--binary", "--full-index", "--no-index", "--", str(old_path), str(new_path), allowed=(0, 1))
        atomic_json(directory / "diff-metadata.json", {"basis": "task-start", "files": metadata,
                    "note": "Git content diff against captured task-start bytes. Symlinks use target text; exact file kinds/modes are recorded here. Review evidence, not an apply-ready patch."})
        return patch

    def _worker_report(self, directory):
        try:
            value = read_json(directory / "last-message.md")
            if (not isinstance(value, dict) or not isinstance(value.get("outcome"), str)
                    or value["outcome"] not in {"done", "decision_required", "blocked"}):
                raise ValueError("Missing valid outcome")
            if not isinstance(value.get("summary"), str) or not value["summary"].strip():
                raise ValueError("Missing summary")
            for field in ("changes", "checks"):
                if not isinstance(value.get(field), list) or not all(isinstance(x, str) for x in value[field]):
                    raise ValueError("Invalid " + field)
            issue = value.get("issue")
            if value["outcome"] == "done":
                if issue is not None:
                    raise ValueError("done cannot retain an unresolved issue")
            elif (not isinstance(issue, dict) or not isinstance(issue.get("kind"), str)
                  or issue["kind"] not in {"engineering_choice", "product_decision", "environment", "permission"}
                  or any(not isinstance(issue.get(k), str) for k in ISSUE_FIELDS)
                  or not issue["detail"].strip() or not issue["evidence"].strip()):
                raise ValueError("Missing concrete issue and evidence")
            return value
        except (OSError, ValueError) as exc:
            return {"outcome": "invalid_report", "summary": str(exc), "changes": [], "checks": [], "issue": None}

    def _prepare_review(self, state):
        directory = self._round_dir(state)
        before = self.snapshot_for(state)
        if before["head"] != state["base_commit"]:
            return self._escalate(state, "HEAD changed during the Ticket; no automatic Git repair was attempted.")
        baseline = read_json(self.ticket_dir(state["id"]) / "baseline.json")
        if before["index_sha256"] != baseline["index_sha256"]:
            return self._escalate(state, "Git index changed during the Ticket; existing staged work was left untouched.")
        changed = self._changed(before, baseline)
        scope = state["spec"].get("allowed_paths")
        if scope:
            outside = [name for name in changed if not self._in_scope(name, scope)]
            if outside:
                return self._escalate(state, "Changes outside Ticket scope: " + ", ".join(outside))
        worker_report = self._worker_report(directory) if (directory / "worker-schema.json").exists() else None
        if worker_report:
            atomic_json(directory / "worker-report.json", worker_report)
        permission_blocked = bool(worker_report and (worker_report.get("issue") or {}).get("kind") == "permission")
        tests = []
        self._progress("stage", stage="validation", round=state["round"], test_total=len(state["spec"]["test_commands"]))
        for i, command in enumerate(state["spec"]["test_commands"], 1):
            argv = [sys.executable if x == "{python}" else x for x in command]
            if permission_blocked:
                tests.append({"argv": argv, "exit_code": None,
                              "skipped": "Worker reported an observed permission failure; the controller did not retry commands in another execution context."})
                self._progress("test", index=i, result="SKIPPED")
                continue
            output = directory / f"test-{i:02d}.stdout.log"
            error = directory / f"test-{i:02d}.stderr.log"
            try:
                self._progress("test", index=i, result="RUNNING")
                code = self._process(state, argv, None, output, error, timeout=self.timeout)
                tests.append({"argv": argv, "exit_code": code, "stdout": str(output), "stderr": str(error)})
                self._progress("test", index=i, result="PASS" if code == 0 else "FAIL")
            except Interrupted:
                raise
            except Exception as exc:
                tests.append({"argv": argv, "exit_code": None, "error": str(exc), "stdout": str(output), "stderr": str(error)})
                self._progress("test", index=i, result="FAIL")
                break
        atomic_json(directory / "tests.json", tests)
        self._progress("stage", stage="evidence")
        if permission_blocked:
            # No test commands ran; reuse before snapshot without a redundant full-repo scan.
            after = before
        else:
            after = self.snapshot_for(state)
            if after["sha256"] != before["sha256"]:
                return self._escalate(state, "Validation changed snapshotted repository files; use tests that leave source unchanged.")
        # _task_diff verifies every changed file's SHA-256 while holding worktree_lock + writer lease.
        patch = self._task_diff(state, baseline, after, changed)
        (directory / "diff.patch").write_bytes(patch)
        atomic_json(directory / "snapshot.json", after)
        state.update(status="REVIEW", expected_sha256=after["sha256"], runner_pid=None, reason=None,
                     recoverable_interruption=False)
        from inbox import review_messages
        packet = {"ticket_id": state["id"], "round": state["round"], "snapshot_sha256": after["sha256"],
                  "worktree": str(self.repo), "base_commit": state["base_commit"], "spec": state["spec"],
                  "session_id": state["session_id"], "changed_files": changed, "tests": tests,
                  "all_tests_passed": len(tests) == len(state["spec"]["test_commands"]) and all(x["exit_code"] == 0 for x in tests),
                  "diff_path": str(directory / "diff.patch"), "summary_path": str(directory / "summary.json"),
                  "diff_basis": "task-start" if state.get("baseline_format") else "HEAD",
                  "diff_metadata_path": str(directory / "diff-metadata.json") if state.get("baseline_format") else None,
                  "worker_report": worker_report, "resolutions": state.get("resolutions", []),
                  "inbox_messages": review_messages(self, state["id"]),
                  "last_message_path": str(directory / "last-message.md"), "previous_review": state.get("last_review")}
        atomic_json(directory / "review-packet.json", packet)
        self.save(state, "review_ready")
        return state

    def packet(self, ticket_id):
        state = self.load(ticket_id)
        if state["status"] != "REVIEW":
            raise WorkflowError("No pending review for this Ticket.")
        if self.snapshot_for(state)["sha256"] != state["expected_sha256"]:
            raise WorkflowError("Review snapshot is stale. Inspect the external change, then refresh the review packet.")
        return read_json(self._round_dir(state) / "review-packet.json")

    def refresh(self, ticket_id):
        with worktree_lock(self.lock):
            state = self.load(ticket_id); self._active(state)
            if state["status"] != "REVIEW":
                raise WorkflowError("Refresh is only valid for a pending review.")
            state["runner_pid"] = os.getpid()
            self.save(state, "review_refresh_started")
            with runtime.acquire_writer_lease(self.workspace, f"{ticket_id}-refresh", allow_strict=True, already_locked=True):
                return self._prepare_review(state)

    def verdict(self, ticket_id, review, source="manual"):
        with worktree_lock(self.lock):
            state = self.load(ticket_id); self._active(state)
            packet = self.packet(ticket_id)
            if not isinstance(review, dict):
                raise WorkflowError("Review must be an object.")
            for key, expected in (("ticket_id", ticket_id), ("round", state["round"]), ("snapshot_sha256", packet["snapshot_sha256"])):
                if review.get(key) != expected or (key == "round" and type(review.get(key)) is not int):
                    raise WorkflowError(f"Review {key} does not match this exact Ticket round/snapshot.")
            decision = review.get("verdict")
            if not isinstance(decision, str) or decision not in {"PASS", "REVISE", "ESCALATE"} or not isinstance(review.get("summary"), str) or not review["summary"].strip():
                raise WorkflowError("Review needs PASS/REVISE/ESCALATE and an explanatory summary.")
            findings = review.get("findings")
            if not isinstance(findings, list) or any(not isinstance(f, dict) or any(not isinstance(f.get(k), str) for k in ("id", "path", "reason", "requested_fix")) for f in findings):
                raise WorkflowError("Findings must include id, path, reason, requested_fix strings.")
            axes = review.get("axes")
            if not isinstance(axes, dict) or any(not isinstance(axes.get(k), str) or not axes[k].strip() for k in ("spec", "standards")):
                raise WorkflowError("Review needs conclusions for both spec and standards.")
            blocker = review.get("blocker")
            if blocker is not None:
                if (decision != "ESCALATE" or not isinstance(blocker, dict)
                        or not isinstance(blocker.get("kind"), str) or blocker["kind"] not in {"product_decision", "environment", "permission"}
                        or any(not isinstance(blocker.get(k), str) for k in ISSUE_FIELDS)
                        or not blocker["detail"].strip() or not blocker["evidence"].strip()):
                    raise WorkflowError("A blocker requires ESCALATE, a concrete category and observed evidence.")
                if blocker["kind"] == "product_decision" and not blocker["question"].strip():
                    raise WorkflowError("A product decision requires a specific user question.")
            worker = packet.get("worker_report")
            if decision == "PASS" and worker and worker["outcome"] != "done":
                raise WorkflowError("PASS cannot hide an unresolved worker issue or invalid report.")
            permission_issue = worker and (worker.get("issue") or {}).get("kind") == "permission"
            if permission_issue and (decision != "ESCALATE" or not blocker or blocker["kind"] != "permission"):
                raise WorkflowError("An observed permission blocker needs ESCALATE with blocker.kind=permission; do not relabel it as a product choice or code defect.")
            if decision == "PASS" and (findings or not packet["all_tests_passed"]):
                raise WorkflowError("PASS requires passing independent test commands and zero unresolved findings.")
            if decision == "REVISE" and not findings:
                raise WorkflowError("REVISE requires at least one actionable finding.")
            record = dict(review, reviewer_source=source, reviewed_at=now())
            if permission_issue:
                record["worker_permission_evidence"] = worker["issue"]["evidence"]
            effective = "ESCALATE" if decision == "REVISE" and state["round"] >= state["max_rounds"] else decision
            record["effective_verdict"] = effective
            atomic_json(self._round_dir(state) / "review.json", record)
            state.update(status=effective, last_review=record, reason="Round limit reached" if effective != decision else review["summary"],
                         stop_origin="review",
                         resolution_allowed=bool(effective == "ESCALATE" and decision == "ESCALATE" and blocker
                                                 and state["session_id"] and state["round"] < state["max_rounds"]))
            self.save(state, "review_recorded")
            return state

    def resolve(self, ticket_id, answer, source, evidence="", expected_round=None):
        """Record a scoped answer/fix. This grants no permissions and never resets the budget."""
        if not isinstance(answer, str) or not answer.strip() or not isinstance(source, str) or source not in {"user", "router"}:
            raise WorkflowError("Resolution needs an explicit answer and source user/router.")
        if not isinstance(evidence, str):
            raise WorkflowError("Resolution evidence must be text.")
        with worktree_lock(self.lock):
            state = self.load(ticket_id); self._active(state)
            if state["status"] != "ESCALATE" or not state.get("resolution_allowed"):
                raise WorkflowError("This stop cannot be reopened by a decision answer; inspect its actual cause.")
            if type(expected_round) is not int or expected_round != state["round"]:
                raise WorkflowError("Resolution must name the current blocked round; an older answer cannot resolve a new question.")
            if state["round"] >= state["max_rounds"] or not state["session_id"]:
                raise WorkflowError("No implementation budget or Session remains for resolution.")
            if pid_alive(state.get("child_pid"), process_group=os.name != "nt"):
                raise WorkflowError("The previous worker is still alive.")
            if self.snapshot_for(state)["sha256"] != state["expected_sha256"]:
                raise WorkflowError("Worktree changed while awaiting resolution; inspect the change before continuing.")
            previous = state.get("last_review") or {}
            blocker = previous.get("blocker") or {}
            kind = blocker.get("kind")
            if kind == "product_decision" and source != "user":
                raise WorkflowError("A product decision requires the user's actual answer.")
            if kind in {"permission", "environment"} and not evidence.strip():
                raise WorkflowError("A permission/environment blocker needs evidence of the observed fix; an answer is not a permission grant.")
            if kind not in {"product_decision", "environment", "permission"}:
                raise WorkflowError("No resolvable blocker is recorded.")
            resolution = {"at": now(), "after_round": state["round"], "kind": kind,
                          "source": source, "answer": answer.strip(), "evidence": evidence.strip()}
            state.setdefault("resolutions", []).append(resolution)
            text = (f"Recorded {source} resolution within the unchanged contract: {answer.strip()}\n"
                    f"Observed evidence: {evidence.strip() or 'User answered the recorded question.'}\n"
                    "Existing host and Codex permissions still apply; this record does not grant new capabilities.")
            state.update(status="REVISE", reason=text, resolution_allowed=False, stop_origin=None,
                         last_review={"verdict": "REVISE", "summary": text,
                                      "findings": previous.get("findings", [])})
            self.save(state, "resolution_recorded")
            return state

    def recover(self, ticket_id):
        with worktree_lock(self.lock):
            state = self.load(ticket_id); self._active(state)
            if state["status"] != "RUNNING" and not state.get("recoverable_interruption"):
                return state
            if state["host"] != socket.gethostname():
                raise WorkflowError("Recovery requires the original host to verify the worker process.")
            if pid_alive(state.get("child_pid"), process_group=os.name != "nt"):
                raise WorkflowError("The saved worker/process group is still alive. No replacement was started.")
            if state.get("runner_pid") != os.getpid() and pid_alive(state.get("runner_pid")):
                raise WorkflowError("The saved controller process is still alive. No replacement was started.")

            import inbox
            ws = self.workspace
            run_id = state.get("run_id")
            if run_id and (runtime.run_dir_for(ws, run_id) / "run.json").is_file():
                ticket_inbox = inbox.InboxTarget(
                    inbox_dir=self.ticket_dir(ticket_id) / "inbox",
                    owner_id=ticket_id,
                    load_state=lambda: self.load(ticket_id),
                    save_state=lambda st, ev=None: self._sync_from_run(state, st, ev) if "run_id" in st else self.save(st, ev),
                    round_number=state["round"],
                )
                runtime.reconcile_run(
                    ws, run_id, codex_cmd=self.codex_cmd, timeout=30,
                    cancel=self.cancel,
                    on_state_sync=lambda rs, ev=None: self._sync_from_run(state, rs, ev),
                    inbox_target=ticket_inbox,
                )
            elif state.get("app_server_pending"):
                # Legacy v0.2 Ticket saved without a Run record.
                from backends.codex_app_server import reconcile_run as legacy_reconcile
                legacy_state = {
                    "thread_id": state.get("session_id"),
                    "active_turn_id": state.get("active_turn_id"),
                    "turn_pending": True,
                    "model_provider": state.get("model_provider"),
                    "sandbox": "workspace-write",
                }
                def legacy_save(ev=None):
                    state.update(
                        app_server_pending=legacy_state["turn_pending"],
                        last_turn_id=legacy_state.get("last_turn_id"),
                        last_turn_status=legacy_state.get("last_turn_status"),
                        active_turn_id=legacy_state.get("active_turn_id"),
                    )
                    self.save(state, ev)
                ticket_inbox = inbox.InboxTarget(
                    inbox_dir=self.ticket_dir(ticket_id) / "inbox",
                    owner_id=ticket_id,
                    load_state=lambda: self.load(ticket_id),
                    save_state=lambda st, ev=None: self.save(st, ev),
                    round_number=state["round"],
                )
                legacy_reconcile(
                    ws=ws, state=legacy_state, save_fn=legacy_save,
                    artifact_dir=self._round_dir(state), inbox_target=ticket_inbox,
                    codex_cmd=self.codex_cmd, timeout=30, cancel=self.cancel,
                )
            if state.get("inbox_window"):
                inbox.recover_window(self, state)

            summary = EventSummary()
            log = self._round_dir(state) / "codex.jsonl"
            if log.exists():
                for line in log.read_bytes().splitlines():
                    summary.feed(line)
            recovered_id = summary.data["session_id"]
            if recovered_id and state["session_id"] and recovered_id != state["session_id"]:
                return self._escalate(state, "Recovered JSONL disagrees with saved Session ID.")
            state["session_id"] = state["session_id"] or recovered_id
            saved_summary = self._round_dir(state) / "summary.json"
            if saved_summary.exists() and state["session_id"]:
                done = read_json(saved_summary)
                if (done.get("exit_code") == 0 and done.get("completed") and not done.get("failed")
                        and not done.get("invalid_lines") and done.get("session_id") == state["session_id"]):
                    state.update(child_pid=None, runner_pid=os.getpid(), recoverable_interruption=True)
                    self.save(state, "completed_worker_recovered")
                    try:
                        with runtime.acquire_writer_lease(ws, f"{ticket_id}-recover", allow_strict=True, already_locked=True):
                            return self._prepare_review(state)
                    except (Exception, KeyboardInterrupt) as exc:
                        return self._escalate(state, str(exc) or type(exc).__name__, recoverable=True)
            if not state["session_id"] or not (state.get("child_pid") or state.get("last_child_pid")):
                return self._escalate(state, "Interrupted before worker/session identity was durably recorded; inspect processes manually.")
            snapshot = self.snapshot_for(state)
            if snapshot["head"] != state["base_commit"]:
                return self._escalate(state, "HEAD changed during interruption.")
            state.update(child_pid=None, runner_pid=None, expected_sha256=snapshot["sha256"],
                         status="REVISE", recoverable_interruption=False,
                         reason="Interrupted invocation. Inspect current files and continue idempotently in the same Session.")
            state["last_review"] = {"verdict": "REVISE", "summary": state["reason"], "findings": []}
            if state["round"] >= state["max_rounds"]:
                return self._escalate(state, "Interrupted at the implementation round limit.")
            self.save(state, "interruption_recovered")
            return state

    def escalate(self, ticket_id, reason):
        with worktree_lock(self.lock):
            state = self.load(ticket_id)
            if state["status"] == "RUNNING" or pid_alive(state.get("child_pid"), process_group=os.name != "nt"):
                raise WorkflowError("Cannot close a running Ticket; stop/reconcile its owned process first.")
            return self._escalate(state, reason)


def doctor(repo, codex_cmd=None, *, require_git=True, check_windows_sandbox=False):
    report = {"controller_version": VERSION, "python": sys.version.split()[0], "platform": sys.platform,
              "python_executable": sys.executable, "git_executable": shutil.which("git"),
              "agy_cli_on_path": shutil.which("agy") or shutil.which("antigravity"),
              "live_exec": "NOT_RUN", "live_resume": "NOT_RUN", "agy_workflow": "NOT_RUN", "checks": {}}
    report["checks"]["git_on_path"] = bool(report["git_executable"])
    report["checks"]["git_invocation"] = False
    if report["git_executable"]:
        try:
            p = subprocess.run([report["git_executable"], "--version"], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=10)
            report["git_version"] = p.stdout.strip()[:150]
            report["checks"]["git_invocation"] = p.returncode == 0
            if p.returncode != 0:
                report["git_error"] = p.stderr.strip()[:300] or f"Git --version exited with code {p.returncode}."
        except (OSError, subprocess.SubprocessError) as exc:
            report["git_error"] = str(exc)
    else:
        report["git_error"] = "Git was not found on PATH. Use an installed Git executable in this environment."
    launcher = None
    try:
        launcher = codex_cmd or resolve_codex()
        report["codex_launcher"] = launcher
        for name, args in (("version", ["--version"]), ("login", ["login", "status"]), ("exec_help", ["exec", "--help"]), ("resume_help", ["exec", "resume", "--help"])):
            p = subprocess.run([*launcher, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
            text = p.stdout + p.stderr
            if name == "version":
                report["codex_version"] = text.strip()[:150]
            elif name == "login":
                report["authentication"] = "ChatGPT" if "ChatGPT" in text else "authenticated" if p.returncode == 0 else "not_authenticated"
            report["checks"][name] = p.returncode == 0 and (all(flag in text for flag in ("--json", "--output-last-message", "--output-schema")) if name.endswith("help") else True)
    except (OSError, subprocess.SubprocessError, WorkflowError) as exc:
        report["codex_error"] = str(exc); report["checks"]["codex"] = False
    task_home = Path(current_codex_home())
    report["codex_home"] = str(task_home)
    for label, path in (("workspace_write", Path(repo).resolve()), ("codex_home_write", task_home)):
        try:
            with tempfile.NamedTemporaryFile(prefix="agy-codex-doctor-", dir=path):
                pass
            report["checks"][label] = True
        except OSError as exc:
            report["checks"][label] = False
            report[label + "_error"] = str(exc)
    if report["checks"]["git_invocation"]:
        try:
            controller = Controller(repo)
            report["worktree"] = str(controller.repo)
            report["state_directory"] = str(controller.root)
            report["checks"]["git_repository"] = True
            report["clean_worktree"] = not bool(git(controller.repo, "status", "--porcelain=v1", "--untracked-files=all").strip())
            report["existing_context"] = {x: (controller.repo / x).exists() for x in ("AGENTS.md", "PLAN.md", "RESUME.md", ".agents/skills")}
        except (OSError, subprocess.SubprocessError, WorkflowError) as exc:
            report["checks"]["git_repository"] = False; report["git_error"] = str(exc)
    else:
        report["checks"]["git_repository"] = False
        report["git_repository_error"] = "Not checked because Git is unavailable; see git_error."
    if not report["checks"]["git_repository"] and not require_git:
        ws = runtime.resolve_workspace(repo)
        report["workspace"] = str(ws.root_path)
        report["state_directory"] = str(ws.state_dir)
    if check_windows_sandbox and launcher and os.name == "nt":
        from backends.codex_app_server import probe_windows_sandbox_readiness
        report["windows_sandbox_readiness"] = probe_windows_sandbox_readiness(Path(repo).resolve(), launcher)
    if require_git:
        report["static_checks_passed"] = all(report["checks"].values())
    else:
        required = {k: v for k, v in report["checks"].items() if not k.startswith("git_")}
        report["static_checks_passed"] = all(required.values())
    return report


def brief(state):
    return {key: state.get(key) for key in ("id", "status", "round", "max_rounds", "session_id", "worktree", "reason")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--codex", help="Native Codex launcher path (default: PATH)")
    parser.add_argument("--timeout", type=int, default=900, help="Per-process timeout in seconds")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    create = sub.add_parser("create"); create.add_argument("--spec", required=True)
    for name in ("run", "status", "packet", "recover", "refresh"):
        p = sub.add_parser(name); p.add_argument("ticket")
        if name == "run":
            p.add_argument("--model")
            p.add_argument("--reasoning-effort")
    verdict = sub.add_parser("verdict"); verdict.add_argument("ticket"); verdict.add_argument("--file", required=True)
    close = sub.add_parser("escalate"); close.add_argument("ticket"); close.add_argument("--reason", required=True)
    resolution = sub.add_parser("resolve"); resolution.add_argument("ticket")
    resolution.add_argument("--answer", required=True); resolution.add_argument("--source", choices=["user", "router"], required=True)
    resolution.add_argument("--evidence", default="")
    resolution.add_argument("--round", dest="expected_round", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        launcher = resolve_codex(args.codex) if args.codex else None
        if args.command == "doctor":
            result = doctor(args.repo, launcher)
            print(encode(result), end="")
            return 0 if result["static_checks_passed"] else 2
        c = Controller(args.repo, launcher, args.timeout)
        if args.command == "create":
            result = c.create(read_json(args.spec))
        elif args.command == "verdict":
            result = c.verdict(args.ticket, read_json(args.file))
        elif args.command == "escalate":
            result = c.escalate(args.ticket, args.reason)
        elif args.command == "resolve":
            result = c.resolve(args.ticket, args.answer, args.source, args.evidence, args.expected_round)
        elif args.command == "status":
            result = c.load(args.ticket)
        elif args.command == "run":
            result = c.run(args.ticket, codex={k: v for k, v in {"model": args.model, "reasoning_effort": args.reasoning_effort}.items() if v is not None})
        else:
            result = getattr(c, args.command)(args.ticket)
        print(encode(result if args.command == "packet" else brief(result)), end="")
        return 2 if result.get("status") == "ESCALATE" else 0
    except (WorkflowError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(encode({"error": str(exc)}), end="")
        return 2


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "_child-gate":
        raise SystemExit(launch_gate(json.loads(sys.argv[2])))
    raise SystemExit(main())
