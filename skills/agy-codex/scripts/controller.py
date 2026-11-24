#!/usr/bin/env python3
"""Local, serial Codex worker controller. Python standard library only."""
from __future__ import annotations

import argparse
import contextlib
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid

VERSION = "0.1.1"
# Support direct and importlib-loaded Controller entry points, including child probes.
_scripts_dir = str(Path(__file__).resolve().parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)
if __name__ == "__main__":
    sys.modules.setdefault("controller", sys.modules[__name__])
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


class WorkflowError(RuntimeError):
    pass


class Interrupted(WorkflowError):
    pass


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _metadata_io(operation):
    """Bounded retry for transient Windows sharing failures during publication.

    Windows readers can briefly reject open/replace while another handle holds
    the file. Keep the atomic rename protocol; never retry JSON parse errors,
    missing files, unrelated I/O failures, or POSIX permission errors.
    """
    for delay in (0.01, 0.02, 0.04, 0.08, 0.16, None):
        try:
            return operation()
        except PermissionError as exc:
            if (sys.platform != "win32" or delay is None
                    or getattr(exc, "winerror", None) not in (None, 5, 32, 33)):
                raise
            time.sleep(delay)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(encode(value)); f.flush(); os.fsync(f.fileno())
        _metadata_io(lambda: os.replace(name, path))
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path):
    path = Path(path)
    return json.loads(_metadata_io(lambda: path.read_text(encoding="utf-8-sig")))


def git(repo, *args, allowed=(0,)):
    env = os.environ.copy()
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    p = subprocess.run(["git", "-c", "core.quotepath=false", "-C", str(repo), *args],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=60)
    if p.returncode not in allowed:
        raise WorkflowError(p.stderr.decode("utf-8", "replace").strip() or f"git failed: {args[0]}")
    return p.stdout


def resolve_codex(executable=None):
    """Do not pass user prompts through a Windows command shell."""
    found = shutil.which(executable or "codex")
    if not found:
        raise WorkflowError("Codex CLI not found on PATH. Install/sign in on the target computer first.")
    path = Path(found).resolve()
    if os.name == "nt" and path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        js = path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        node = shutil.which("node")
        if js.is_file() and node:
            return [node, str(js)]
        raise WorkflowError("Found a Windows shell shim, but not its standard npm codex.js launcher. "
                            "Use --codex with a native codex.exe, or repair the npm installation.")
    return [str(path)]


def pid_alive(pid, process_group=False):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # Unknown/access denied is treated as live.
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        if process_group:
            os.killpg(pid, 0)
        else:
            os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def stop_process(p):
    """Stop an owned group, including descendants after the leader has exited."""
    if os.name == "nt":
        job = getattr(p, "_superagent_job", None)
        if job:
            job.close()
        elif p.poll() is None:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(p.pid, signal.SIGTERM)
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        # The leader may already have exited; SIGKILL still targets surviving descendants.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(p.pid, signal.SIGKILL)
    p.wait(timeout=15)


def launch_gate(argv):
    """Wait until the Windows parent has assigned this process to its Job Object."""
    if os.read(0, 1) != b"1":
        return 125
    # Unbuffered os.read above consumes only the gate byte. The real command receives
    # the remaining stdin bytes intact and inherits the already-associated job.
    return subprocess.call(argv)


class WindowsJob:
    """Keep the entire Windows worker tree owned by this controller process."""
    def __init__(self, p):
        import ctypes
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        kernel.CreateJobObjectW.restype = w.HANDLE
        kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel, self.handle = kernel, kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise WorkflowError("Windows could not create an owned process job.")
        limits = Extended(); limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(self.handle, w.HANDLE(int(p._handle))):
            error = ctypes.get_last_error()
            self.close()
            raise WorkflowError(f"Windows process-tree containment failed (error {error}); worker stopped.")

    def close(self):
        if self.handle:
            self.kernel.TerminateJobObject(self.handle, 1)
            self.kernel.CloseHandle(self.handle)
            self.handle = None


@contextlib.contextmanager
def worktree_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as f:
        f.seek(0, os.SEEK_END)
        if f.tell() == 0:
            f.write(b"0"); f.flush()
        f.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WorkflowError("This worktree already has a running controller; no second worker was started.") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


class EventSummary:
    def __init__(self):
        self.data = {"session_id": None, "completed": False, "failed": False,
                     "invalid_lines": 0, "commands": [], "changed_files": [], "errors": [], "usage": {}}

    def feed(self, raw):
        try:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError("event must be an object")
        except (ValueError, UnicodeError):
            self.data["invalid_lines"] += 1
            return None
        kind = event.get("type")
        if kind == "thread.started":
            session = event.get("thread_id")
            if not isinstance(session, str) or not session:
                self.data["invalid_lines"] += 1
            elif self.data["session_id"] and self.data["session_id"] != session:
                self.data["failed"] = True
                self.data["errors"].append("Multiple session IDs in one invocation")
            else:
                self.data["session_id"] = session
        elif kind == "turn.completed":
            self.data["completed"] = True
            self.data["usage"] = event.get("usage", {})
        elif kind in {"turn.failed", "error"}:
            if kind == "turn.failed":
                self.data["failed"] = True
            self.data["errors"].append(str(event.get("error", event.get("message", event)))[:1200])
        elif kind == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "command_execution":
                self.data["commands"].append({k: item.get(k) for k in ("command", "exit_code", "status")})
            elif item.get("type") == "file_change":
                self.data["changed_files"].extend(c.get("path", "") for c in item.get("changes", []))
        return event


def codex_settings(*layers):
    """Merge explicit worker settings, without modifying Codex's own configuration."""
    result = {}
    for layer in layers:
        if layer is None:
            continue
        if not isinstance(layer, dict) or set(layer) - {"model", "reasoning_effort", "model_provider"}:
            raise WorkflowError("codex must contain only model, reasoning_effort and/or model_provider.")
        for key, value in layer.items():
            pattern = r"[A-Za-z0-9][A-Za-z0-9._:/+\-]*" if key in ("model", "model_provider") else r"[a-z][a-z0-9_\-]*"
            if not isinstance(value, str) or not re.fullmatch(pattern, value):
                raise WorkflowError(f"Invalid codex {key}: expected a nonempty identifier.")
            result[key] = value
    return result


def configured_codex(root):
    """Read optional plugin defaults; project metadata overrides global defaults."""
    from config import load
    from routing import select
    return codex_settings(select(root), load(root)["codex"])


def codex_exec_flags(settings):
    settings = codex_settings(settings)
    flags = []
    if "model" in settings:
        flags += ["-m", settings["model"]]
    if "reasoning_effort" in settings:
        flags += ["-c", "model_reasoning_effort=" + json.dumps(settings["reasoning_effort"])]
    if "model_provider" in settings:
        flags += ["-c", "model_provider=" + json.dumps(settings["model_provider"])]
    return flags


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
            # A missing literal target may later become a directory prefix. Its
            # Git-visible children are already listed; never walk ignored trees.
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
            if expected_sha256 is not None and self.snapshot(previous_paths)["sha256"] != expected_sha256:
                raise WorkflowError("Worktree changed between plan steps; inspect the change before starting the next task.")
            state = {"schema_version": 2, "baseline_format": "files-v1", "id": ticket_id, "spec": spec, "status": "READY", "round": 0,
                     "snapshot_paths": snapshot_paths,
                     "max_rounds": limit, "codex_requested": worker_config, "session_id": None, "worktree": str(self.repo), "git_dir": str(self.gitdir),
                     "codex_home": str(Path(os.environ.get("CODEX_HOME", str(Path.home()/'.codex'))).resolve()),
                     "base_commit": snap["head"], "expected_sha256": snap["sha256"], "created_at": now(),
                     "host": socket.gethostname(), "runner_pid": None, "child_pid": None, "last_review": None,
                     "reason": None, "resolutions": [], "resolution_allowed": False}
            directory.mkdir(parents=True)
            try:
                self._capture_files(snap, directory/"baseline-files", [n for n in snap["files"] if self._in_scope(n, scope)])
                if self.snapshot(snapshot_paths)["sha256"] != snap["sha256"]:
                    raise WorkflowError("Worktree changed while capturing the task baseline; retry after the other writer finishes.")
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
        return directory / ("contract.json" if (directory/"contract.json").exists() else "ticket.json")

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
        if self.cancel.is_set():
            raise Interrupted("Execution was canceled before process startup; use recover to continue.")
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        opts = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        q = queue.Queue()
        with Path(stdout_path).open("wb") as out, Path(stderr_path).open("wb") as err:
            gated = os.name == "nt"
            launch_argv = [sys.executable, "-B", str(Path(__file__).resolve()), "_child-gate", json.dumps(argv)] if gated else argv
            p = subprocess.Popen(launch_argv, cwd=self.repo, env=env, stdin=subprocess.PIPE if prompt is not None or gated else subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=err, **opts)
            state["child_pid"] = p.pid
            t = None
            writer = None
            try:
                if os.name == "nt":
                    p._superagent_job = WindowsJob(p)
                self.save(state, "process_started")
                def reader():
                    try:
                        for line in iter(p.stdout.readline, b""):
                            q.put(line)
                    finally:
                        q.put(None)
                t = threading.Thread(target=reader, daemon=True); t.start()
                if prompt is not None or gated:
                    def send_input():
                        try:
                            p.stdin.write((b"1" if gated else b"") + (prompt or "").encode("utf-8")); p.stdin.close()
                        except (BrokenPipeError, OSError, ValueError):
                            pass
                    writer = threading.Thread(target=send_input, daemon=True); writer.start()
                deadline = time.monotonic() + (timeout or self.timeout)
                stream_done = False
                group_stopped = False
                while not stream_done or p.poll() is None:
                    if self.cancel.is_set():
                        raise Interrupted("Worker was canceled; use recover to resume its saved Session.")
                    if time.monotonic() > deadline:
                        raise Interrupted("Process timeout; the owned process tree was stopped. Use recover to continue.")
                    if p.poll() is not None and not group_stopped:
                        stop_process(p)
                        group_stopped = True
                    try:
                        line = q.get(timeout=0.15)
                    except queue.Empty:
                        continue
                    if line is None:
                        stream_done = True
                    else:
                        out.write(line); out.flush()
                        if on_line:
                            on_line(line)
                result = p.wait()
                return result
            finally:
                stop_process(p)
                if t:
                    t.join(timeout=2)
                if writer:
                    writer.join(timeout=2)
                p.stdout.close()
                if p.stdin and not p.stdin.closed:
                    p.stdin.close()
                # Keep the last owned PID as recovery evidence, not as a claim it is still live.
                state["last_child_pid"] = p.pid
                state["child_pid"] = None
                self.save(state)

    def _escalate(self, state, reason, recoverable=False):
        state.update(status="ESCALATE", reason=reason, runner_pid=None,
                     recoverable_interruption=recoverable, resolution_allowed=False, stop_origin="controller")
        self.save(state, "escalated")
        return state

    def run(self, ticket_id, *, codex=None):
        with worktree_lock(self.lock):
            state = self.load(ticket_id)
            self._active(state)
            if state["status"] not in {"READY", "REVISE"}:
                raise WorkflowError(f"Cannot run from {state['status']}; use status, review, or recover.")
            if state["round"] >= state["max_rounds"]:
                return self._escalate(state, "Implementation round limit reached; no fourth worker call.")
            current_home = str(Path(os.environ.get("CODEX_HOME", str(Path.home()/'.codex'))).resolve())
            if state["host"] != socket.gethostname() or state["codex_home"] != current_home:
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
            if backend == "exec" and policy(self.root)["allowed_providers"]:
                verify_provider(self.root, requested.get("model_provider"))
            state.update(codex_requested=requested, status="RUNNING", round=state["round"]+1, runner_pid=os.getpid(), reason=None,
                         recoverable_interruption=False, resolution_allowed=False, stop_origin=None)
            directory = self._round_dir(state)
            if directory.exists() and any(directory.iterdir()):
                raise WorkflowError("The next round already contains evidence. Inspect it before recovery; it will not be overwritten.")
            directory.mkdir(parents=True, exist_ok=True)
            self.save(state, "implementation_started")
            prompt = self._prompt(state)
            (directory / "prompt.txt").write_text(prompt, encoding="utf-8")
            atomic_json(directory/"worker-schema.json", WORKER_SCHEMA)
            summary = EventSummary()
            def on_line(raw):
                event = summary.feed(raw)
                if event and event.get("type") == "thread.started":
                    session_id = event.get("thread_id")
                    if not isinstance(session_id, str) or not session_id:
                        raise WorkflowError("Codex emitted an invalid session ID.")
                    if state["session_id"] and state["session_id"] != session_id:
                        raise WorkflowError("Resumed Codex returned a different Session ID. Original mapping retained.")
                    state["session_id"] = session_id
                    self.save(state, "session_bound")
                if event and event.get("type") in {"item.started", "item.completed"}:
                    item = event.get("item") or {}
                    if item.get("type") == "command_execution":
                        self._progress("command", completed=event["type"] == "item.completed")
                    elif item.get("type") == "file_change" and event["type"] == "item.completed":
                        self._progress("files", paths=[change.get("path", "") for change in item.get("changes", [])])
            try:
                self._progress("stage", stage="worker", round=state["round"])
                from backends import codex_exec, codex_app_server
                if backend == "exec":
                    rc = codex_exec.run(self, state, launcher, requested, directory, prompt, on_line)
                else:
                    try:
                        rc = codex_app_server.run(self, state, launcher, requested, directory, prompt, on_line)
                    except codex_app_server.StartupUnavailable as exc:
                        if backend != "auto" or state.get("app_server_pending"):
                            raise
                        if policy(self.root)["allowed_providers"]:
                            verify_provider(self.root, requested.get("model_provider"))
                        atomic_json(directory/"backend-fallback.json", {"reason": str(exc),
                                    "from": "app-server", "to": "exec", "before_turn": True})
                        rc = codex_exec.run(self, state, launcher, requested, directory, prompt, on_line)
                summary.data["exit_code"] = rc
                atomic_json(directory / "summary.json", summary.data)
                if (rc or not summary.data["completed"] or summary.data["failed"] or summary.data["invalid_lines"]
                        or not state["session_id"] or summary.data["session_id"] != state["session_id"]):
                    return self._escalate(state, "Codex execution did not complete cleanly; inspect summary.json and stderr.log.")
                return self._prepare_review(state)
            except (Exception, KeyboardInterrupt) as exc:
                summary.data["controller_error"] = str(exc) or type(exc).__name__
                atomic_json(directory / "summary.json", summary.data)
                completed_worker = (summary.data.get("exit_code") == 0 and summary.data["completed"]
                                    and not summary.data["failed"] and not summary.data["invalid_lines"]
                                    and summary.data["session_id"] == state["session_id"])
                recoverable = bool(state["session_id"]) and (isinstance(exc, (Interrupted, KeyboardInterrupt)) or completed_worker or state.get("app_server_pending"))
                return self._escalate(state, summary.data["controller_error"], recoverable=recoverable)

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
            source, target = self.repo/name, destination/name
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
            # v0.1/v0.2 tickets were required to start from a clean HEAD.
            patch = git(self.repo, "diff", "--no-ext-diff", "--no-textconv", "--binary", "--full-index", state["base_commit"], "--")
            for name in git(self.repo, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
                if name:
                    patch += git(self.repo, "diff", "--no-ext-diff", "--no-textconv", "--binary", "--no-index", "--", os.devnull, os.fsdecode(name), allowed=(0, 1))
            return patch
        before_dir = self.ticket_dir(state["id"])/"baseline-files"
        after_dir = directory/"changed-files"
        self._capture_files(snapshot, after_dir, changed)
        patch = b""
        metadata = []
        for name in changed:
            old = baseline["files"].get(name, {"kind": "missing"})
            new = snapshot["files"].get(name, {"kind": "missing"})
            old_path = before_dir/name if old.get("kind") != "missing" else Path(os.devnull)
            new_path = after_dir/name if new.get("kind") != "missing" else Path(os.devnull)
            if old.get("sha256") and hashlib.sha256(old_path.read_bytes()).hexdigest() != old["sha256"]:
                raise WorkflowError("Saved baseline content changed: " + name)
            if old.get("kind") == "symlink" and old_path.read_bytes() != os.fsencode(old["target"]):
                raise WorkflowError("Saved baseline link changed: " + name)
            metadata.append({"path": name, "before": old, "after": new})
            patch += ("\n# TASK PATH " + json.dumps(name, ensure_ascii=False) + "\n").encode("utf-8")
            patch += git(self.repo, "diff", "--no-ext-diff", "--no-textconv", "--binary", "--full-index", "--no-index", "--", str(old_path), str(new_path), allowed=(0, 1))
        atomic_json(directory/"diff-metadata.json", {"basis": "task-start", "files": metadata,
                    "note": "Git content diff against captured task-start bytes. Symlinks use target text; exact file kinds/modes are recorded here. Review evidence, not an apply-ready patch."})
        return patch

    def _worker_report(self, directory):
        try:
            value = read_json(directory/"last-message.md")
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
            # A malformed report is a reviewable defect, not proof of completion.
            return {"outcome": "invalid_report", "summary": str(exc), "changes": [], "checks": [], "issue": None}

    def _prepare_review(self, state):
        directory = self._round_dir(state)
        before = self.snapshot_for(state)
        if before["head"] != state["base_commit"]:
            return self._escalate(state, "HEAD changed during the Ticket; no automatic Git repair was attempted.")
        baseline = read_json(self.ticket_dir(state["id"])/"baseline.json")
        if before["index_sha256"] != baseline["index_sha256"]:
            return self._escalate(state, "Git index changed during the Ticket; existing staged work was left untouched.")
        changed = self._changed(before, baseline)
        scope = state["spec"].get("allowed_paths")
        if scope:
            outside = [name for name in changed if not self._in_scope(name, scope)]
            if outside:
                return self._escalate(state, "Changes outside Ticket scope: " + ", ".join(outside))
        worker_report = self._worker_report(directory) if (directory/"worker-schema.json").exists() else None
        if worker_report:
            atomic_json(directory/"worker-report.json", worker_report)
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
        atomic_json(directory/"tests.json", tests)
        self._progress("stage", stage="evidence")
        after = self.snapshot_for(state)
        if after["sha256"] != before["sha256"]:
            return self._escalate(state, "Validation changed snapshotted repository files; use tests that leave source unchanged.")
        patch = self._task_diff(state, baseline, after, changed)
        if self.snapshot_for(state)["sha256"] != after["sha256"]:
            raise WorkflowError("Worktree changed while capturing review evidence.")
        (directory/"diff.patch").write_bytes(patch)
        atomic_json(directory/"snapshot.json", after)
        state.update(status="REVIEW", expected_sha256=after["sha256"], runner_pid=None, reason=None,
                     recoverable_interruption=False)
        from inbox import review_messages
        packet = {"ticket_id": state["id"], "round": state["round"], "snapshot_sha256": after["sha256"],
                  "worktree": str(self.repo), "base_commit": state["base_commit"], "spec": state["spec"],
                  "session_id": state["session_id"], "changed_files": changed, "tests": tests,
                  "all_tests_passed": len(tests) == len(state["spec"]["test_commands"]) and all(x["exit_code"] == 0 for x in tests),
                  "diff_path": str(directory/"diff.patch"), "summary_path": str(directory/"summary.json"),
                  "diff_basis": "task-start" if state.get("baseline_format") else "HEAD",
                  "diff_metadata_path": str(directory/"diff-metadata.json") if state.get("baseline_format") else None,
                  "worker_report": worker_report, "resolutions": state.get("resolutions", []),
                  "inbox_messages": review_messages(self, state["id"]),
                  "last_message_path": str(directory/"last-message.md"), "previous_review": state.get("last_review")}
        atomic_json(directory/"review-packet.json", packet)
        self.save(state, "review_ready")
        return state

    def packet(self, ticket_id):
        state = self.load(ticket_id)
        if state["status"] != "REVIEW":
            raise WorkflowError("No pending review for this Ticket.")
        if self.snapshot_for(state)["sha256"] != state["expected_sha256"]:
            raise WorkflowError("Review snapshot is stale. Inspect the external change, then refresh the review packet.")
        return read_json(self._round_dir(state)/"review-packet.json")

    def refresh(self, ticket_id):
        with worktree_lock(self.lock):
            state = self.load(ticket_id); self._active(state)
            if state["status"] != "REVIEW":
                raise WorkflowError("Refresh is only valid for a pending review.")
            state["runner_pid"] = os.getpid()
            self.save(state, "review_refresh_started")
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
            atomic_json(self._round_dir(state)/"review.json", record)
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
            if state.get("app_server_pending"):
                from backends.codex_app_server import reconcile
                reconcile(self, state)
            if state.get("inbox_window"):
                from inbox import recover_window
                recover_window(self, state)
            summary = EventSummary()
            log = self._round_dir(state)/"codex.jsonl"
            if log.exists():
                for line in log.read_bytes().splitlines():
                    summary.feed(line)
            recovered_id = summary.data["session_id"]
            if recovered_id and state["session_id"] and recovered_id != state["session_id"]:
                return self._escalate(state, "Recovered JSONL disagrees with saved Session ID.")
            state["session_id"] = state["session_id"] or recovered_id
            saved_summary = self._round_dir(state)/"summary.json"
            if saved_summary.exists() and state["session_id"]:
                done = read_json(saved_summary)
                if (done.get("exit_code") == 0 and done.get("completed") and not done.get("failed")
                        and not done.get("invalid_lines") and done.get("session_id") == state["session_id"]):
                    # The implementation finished; only validation/publication of its review packet
                    # was interrupted. Re-run validation in the same round, without another worker.
                    state.update(child_pid=None, runner_pid=os.getpid(), recoverable_interruption=True)
                    self.save(state, "completed_worker_recovered")
                    try:
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


def doctor(repo, codex_cmd=None):
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
    task_home = Path(os.environ.get("CODEX_HOME", str(Path.home()/'.codex'))).resolve()
    report["codex_home"] = str(task_home)
    for label, path in (("workspace_write", Path(repo).resolve()), ("codex_home_write", task_home)):
        try:
            with tempfile.NamedTemporaryFile(prefix="agy-codex-doctor-", dir=path):
                pass
            report["checks"][label] = True
        except OSError as exc:
            report["checks"][label] = False
            report[label+"_error"] = str(exc)
    if report["checks"]["git_invocation"]:
        try:
            controller = Controller(repo)
            report["worktree"] = str(controller.repo)
            report["state_directory"] = str(controller.root)
            report["checks"]["git_repository"] = True
            report["clean_worktree"] = not bool(git(controller.repo, "status", "--porcelain=v1", "--untracked-files=all").strip())
            report["existing_context"] = {x: (controller.repo/x).exists() for x in ("AGENTS.md", "PLAN.md", "RESUME.md", ".agents/skills")}
        except (OSError, subprocess.SubprocessError, WorkflowError) as exc:
            report["checks"]["git_repository"] = False; report["git_error"] = str(exc)
    else:
        report["checks"]["git_repository"] = False
        report["git_repository_error"] = "Not checked because Git is unavailable; see git_error."
    report["static_checks_passed"] = all(report["checks"].values())
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
