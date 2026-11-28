#!/usr/bin/env python3
"""Shared Codex execution runtime (Git & non-Git). Python standard library only."""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

VERSION = "0.3.0"
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")

_scripts_dir = str(Path(__file__).resolve().parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)


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
    """Keep the entire Windows worker tree owned by this process."""
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
def worktree_lock(path, *, wait_seconds=0.0):
    """Advisory OS file lock. Non-blocking by default; optional bounded wait for short locks."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as f:
        f.seek(0, os.SEEK_END)
        if f.tell() == 0:
            f.write(b"0"); f.flush()
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            f.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise WorkflowError("This worktree already has a running controller; no second worker was started.") from exc
                time.sleep(0.02)
        try:
            yield
        finally:
            if os.name == "nt":
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


def _lock_is_free(path):
    path = Path(path)
    if not path.exists():
        return True
    try:
        with worktree_lock(path):
            return True
    except (WorkflowError, OSError):
        return False


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
    """Read optional plugin defaults; workspace metadata overrides global defaults."""
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


def validate_sandbox(mode):
    if mode not in SANDBOX_MODES:
        raise WorkflowError(f"sandbox must be one of {', '.join(SANDBOX_MODES)}.")
    return mode


def current_codex_home():
    return str(Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).resolve())


def plugin_state_root():
    custom = os.environ.get("AGY_CODEX_STATE_DIR")
    if custom:
        return Path(custom).expanduser().resolve()
    return (Path.home() / ".gemini" / "config" / "agy-codex").resolve()


def _registry_root():
    # Use the same global registry root unless explicitly overridden for isolated tests.
    return plugin_state_root()


def norm_path_key(path):
    resolved = str(Path(path).resolve())
    return os.path.normcase(resolved)


def _norm_parts(path):
    return Path(norm_path_key(path)).parts


def paths_overlap(a, b):
    """True if path a and path b are equal or one is an ancestor of the other (by components)."""
    pa, pb = _norm_parts(a), _norm_parts(b)
    if not pa or not pb:
        return False
    n = min(len(pa), len(pb))
    return pa[:n] == pb[:n]


@dataclass(frozen=True)
class Workspace:
    cwd: Path
    root_path: Path
    git_dir: Path | None
    state_dir: Path
    lock_path: Path
    workspace_key: str
    is_git: bool


def resolve_workspace(cwd="."):
    candidate = Path(cwd).expanduser().resolve()
    if not candidate.is_dir():
        raise WorkflowError(f"Workspace directory does not exist: {candidate}")
    try:
        repo = Path(git(candidate, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        gitdir = Path(git(repo, "rev-parse", "--absolute-git-dir").decode().strip()).resolve()
        state_dir = gitdir / "superagent"
        key = "git:" + hashlib.sha256(norm_path_key(gitdir).encode("utf-8")).hexdigest()[:16]
        return Workspace(
            cwd=candidate, root_path=repo, git_dir=gitdir,
            state_dir=state_dir, lock_path=state_dir / "worktree.lock",
            workspace_key=key, is_git=True,
        )
    except (WorkflowError, OSError, subprocess.SubprocessError):
        path_hash = hashlib.sha256(norm_path_key(candidate).encode("utf-8")).hexdigest()[:16]
        state_dir = plugin_state_root() / "workspaces" / path_hash
        return Workspace(
            cwd=candidate, root_path=candidate, git_dir=None,
            state_dir=state_dir, lock_path=state_dir / "workspace.lock",
            workspace_key="dir:" + path_hash, is_git=False,
        )


def _persist_workspace_meta(ws):
    if not ws.is_git:
        atomic_json(ws.state_dir / "workspace.json", {
            "schema_version": 1,
            "workspace_key": ws.workspace_key,
            "cwd": str(ws.root_path),
            "norm_cwd": norm_path_key(ws.root_path),
            "is_git": False,
        })


def _check_strict_occupancy(ws):
    """Prevent lightweight writes from silently bypassing an active/recoverable Strict Ticket or Plan."""
    if not ws.is_git:
        return
    active_path = ws.state_dir / "active.json"
    if active_path.exists():
        tid = read_json(active_path).get("ticket_id")
        tpath = ws.state_dir / "tickets" / str(tid) / "ticket.json" if tid else None
        if tpath and tpath.is_file():
            tstate = read_json(tpath)
            if (tstate.get("status") not in {"PASS", "ESCALATE"}
                    or tstate.get("resolution_allowed")
                    or tstate.get("recoverable_interruption")
                    or pid_alive(tstate.get("child_pid"), process_group=os.name != "nt")):
                raise WorkflowError(
                    f"Worktree is reserved by active Strict Ticket {tstate['id']} ({tstate.get('status')}). "
                    "Continue, resolve, or close it before starting a lightweight write run."
                )
    plan_ptr = ws.state_dir / "active-plan.json"
    if plan_ptr.exists():
        plan_id = read_json(plan_ptr).get("plan_id")
        ppath = ws.state_dir / "plans" / str(plan_id) / "plan.json" if plan_id else None
        if ppath and ppath.is_file():
            plan = read_json(ppath)
            if not plan.get("stopped"):
                for entry in plan.get("tasks", []):
                    tpath = ws.state_dir / "tickets" / entry["ticket_id"] / "ticket.json"
                    if not tpath.is_file() or read_json(tpath).get("status") != "PASS":
                        raise WorkflowError(
                            f"Worktree is reserved by unfinished Strict Plan {plan['id']}. "
                            "Continue or explicitly stop that plan before starting a lightweight write run."
                        )


@contextlib.contextmanager
def acquire_writer_lease(ws, run_id, *, sandbox="workspace-write", allow_strict=False, already_locked=False):
    """Atomic single-writer lease across Git and non-Git workspaces, guarding ancestor/descendant overlap."""
    if sandbox == "read-only":
        yield
        return
    reg_root = _registry_root()
    reg_lock = reg_root / "registry.lock"
    reg_file = reg_root / "active-writers.json"
    host = socket.gethostname()
    lock_cm = None

    with worktree_lock(reg_lock, wait_seconds=5.0):
        registry = read_json(reg_file) if reg_file.exists() else {}
        if not isinstance(registry, dict):
            registry = {}
        cleaned = {}
        for key, entry in registry.items():
            if not isinstance(entry, dict) or not entry.get("root_path"):
                continue
            if key == ws.workspace_key and already_locked:
                cleaned[key] = entry
                continue
            same_host = entry.get("host") == host
            dead_pid = same_host and not pid_alive(entry.get("pid"))
            lock_free = _lock_is_free(entry.get("lock_path", "")) if entry.get("lock_path") else False
            if dead_pid and lock_free:
                continue
            cleaned[key] = entry

        for key, entry in cleaned.items():
            if key == ws.workspace_key and already_locked:
                continue
            if paths_overlap(ws.root_path, Path(entry["root_path"])):
                raise WorkflowError(
                    f"Overlapping workspace already has an active writer ({entry.get('run_id') or key} at {entry['root_path']}); "
                    "wait for it to finish before starting another write run."
                )

        if not already_locked:
            lock_cm = worktree_lock(ws.lock_path)
            lock_cm.__enter__()
        try:
            if not allow_strict:
                _check_strict_occupancy(ws)
            cleaned[ws.workspace_key] = {
                "workspace_key": ws.workspace_key,
                "root_path": str(ws.root_path),
                "lock_path": str(ws.lock_path),
                "run_id": run_id,
                "pid": os.getpid(),
                "host": host,
                "is_git": ws.is_git,
                "started_at": now(),
            }
            atomic_json(reg_file, cleaned)
        except BaseException:
            if lock_cm is not None:
                lock_cm.__exit__(*sys.exc_info())
            raise

    try:
        yield
    finally:
        try:
            with worktree_lock(reg_lock, wait_seconds=5.0):
                registry = read_json(reg_file) if reg_file.exists() else {}
                if isinstance(registry, dict):
                    existing = registry.get(ws.workspace_key)
                    if isinstance(existing, dict) and existing.get("run_id") == run_id:
                        registry.pop(ws.workspace_key, None)
                        atomic_json(reg_file, registry)
        finally:
            if lock_cm is not None:
                lock_cm.__exit__(None, None, None)


def run_process(cwd, argv, prompt, stdout_path, stderr_path, *,
                on_line=None, on_pid=None, timeout=900, cancel=None):
    """Run an owned child process tree with Windows Job containment or POSIX process group."""
    if cancel is not None and cancel.is_set():
        raise Interrupted("Execution was canceled before process startup; inspect saved state before continuing.")
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    opts = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    q = queue.Queue()
    Path(stdout_path).parent.mkdir(parents=True, exist_ok=True)
    Path(stderr_path).parent.mkdir(parents=True, exist_ok=True)
    with Path(stdout_path).open("wb") as out, Path(stderr_path).open("wb") as err:
        gated = os.name == "nt"
        launch_argv = [sys.executable, "-B", str(Path(__file__).resolve()), "_child-gate", json.dumps(argv)] if gated else argv
        p = subprocess.Popen(
            launch_argv, cwd=cwd, env=env,
            stdin=subprocess.PIPE if prompt is not None or gated else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=err, **opts,
        )
        t = None
        writer = None
        try:
            if os.name == "nt":
                p._superagent_job = WindowsJob(p)
            if on_pid:
                on_pid(p.pid, True)
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
                        p.stdin.write((b"1" if gated else b"") + (prompt or "").encode("utf-8"))
                        p.stdin.close()
                    except (BrokenPipeError, OSError, ValueError):
                        pass
                writer = threading.Thread(target=send_input, daemon=True); writer.start()
            deadline = time.monotonic() + (timeout or 900)
            stream_done = False
            group_stopped = False
            while not stream_done or p.poll() is None:
                if cancel is not None and cancel.is_set():
                    raise Interrupted("Worker was canceled; inspect saved state to resume its Session.")
                if time.monotonic() > deadline:
                    raise Interrupted("Process timeout; the owned process tree was stopped. Inspect saved state to continue.")
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
            return p.wait()
        finally:
            stop_process(p)
            if t:
                t.join(timeout=2)
            if writer:
                writer.join(timeout=2)
            p.stdout.close()
            if p.stdin and not p.stdin.closed:
                p.stdin.close()
            if on_pid:
                on_pid(p.pid, False)


def _valid_run_id(run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
        raise WorkflowError("Run ID must contain only letters, digits, underscore, hyphen (1-64 characters).")
    return run_id


def run_dir_for(ws, run_id):
    return ws.state_dir / "runs" / _valid_run_id(run_id)


def load_run(ws, run_id):
    return read_json(run_dir_for(ws, run_id) / "run.json")


def save_run(ws, state, event=None):
    state["updated_at"] = now()
    directory = run_dir_for(ws, state["run_id"])
    atomic_json(directory / "run.json", state)
    if event:
        with (directory / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "at": now(), "event": event, "status": state.get("status"),
                "thread_id": state.get("thread_id"), "turn_id": state.get("active_turn_id"),
            }, ensure_ascii=False) + "\n")
            f.flush(); os.fsync(f.fileno())


def list_runs(ws, *, owner_kind=None, limit=20):
    runs_root = ws.state_dir / "runs"
    if not runs_root.is_dir():
        return []
    items = []
    for path in runs_root.glob("*/run.json"):
        try:
            data = read_json(path)
        except (OSError, ValueError):
            continue
        if owner_kind and data.get("owner_kind") != owner_kind:
            continue
        items.append(data)
    items.sort(key=lambda r: (r.get("started_at", ""), r.get("run_id", "")), reverse=True)
    return items[:limit] if limit else items


def latest_run(ws, *, owner_kind=None):
    if owner_kind is None:
        ptr = ws.state_dir / "latest-run.json"
        if ptr.exists():
            rid = read_json(ptr).get("run_id")
            if rid:
                path = run_dir_for(ws, rid) / "run.json"
                if path.is_file():
                    return read_json(path)
    items = list_runs(ws, owner_kind=owner_kind, limit=1)
    return items[0] if items else None


def _ensure_reconcilable_chain(ws, runs_for_thread):
    for r in runs_for_thread:
        if r.get("turn_pending") or r.get("status") in {"running", "unknown"}:
            raise WorkflowError(
                f"Run {r['run_id']} on this Thread is {r.get('status')} (turn_pending={bool(r.get('turn_pending'))}). "
                "Reconcile or finish it before starting another follow-up Run."
            )


def resolve_resume(ws, resume_spec, *, requested_provider=None):
    """Resolve --resume <RUN_ID|THREAD_ID|last> with strict lineage and provenance checks."""
    if not resume_spec:
        return {"thread_id": None, "continued_from": None, "prior_provider": None}
    if not isinstance(resume_spec, str) or not resume_spec.strip():
        raise WorkflowError("Resume target must be a nonempty Run ID, Thread ID, or 'last'.")
    target = resume_spec.strip()
    all_runs = list_runs(ws, limit=0)
    host = socket.gethostname()
    codex_home = current_codex_home()

    def _validate_run(run):
        if run.get("host") and run["host"] != host:
            raise WorkflowError(f"Run {run['run_id']} belongs to a different host ({run['host']}).")
        if run.get("codex_home") and run["codex_home"] != codex_home:
            raise WorkflowError(f"Run {run['run_id']} used a different CODEX_HOME; cannot safely resume.")
        if not run.get("thread_id"):
            raise WorkflowError(f"Run {run['run_id']} has no recorded native Thread ID to resume.")
        same_thread = [r for r in all_runs if r.get("thread_id") == run["thread_id"]]
        _ensure_reconcilable_chain(ws, same_thread)
        prior_provider = run.get("model_provider")
        if prior_provider and requested_provider not in (None, prior_provider):
            raise WorkflowError("Cannot change provider inside an existing Session.")
        return {"thread_id": run["thread_id"], "continued_from": run["run_id"], "prior_provider": prior_provider}

    if target == "last":
        if not all_runs:
            raise WorkflowError("No saved Run exists in this workspace to resume.")
        newest = all_runs[0]
        if newest.get("turn_pending") or newest.get("status") in {"running", "unknown"}:
            raise WorkflowError(
                f"Latest Run {newest['run_id']} is {newest.get('status')} (turn_pending={bool(newest.get('turn_pending'))}). "
                "Cannot skip an unreconciled Run; reconcile it first."
            )
        return _validate_run(newest)

    by_id = next((r for r in all_runs if r.get("run_id") == target), None)
    if by_id is not None:
        return _validate_run(by_id)

    # Otherwise treat as native Thread ID; if owned locally, enforce full lineage checks.
    by_thread = [r for r in all_runs if r.get("thread_id") == target]
    if by_thread:
        return _validate_run(by_thread[0])

    # External native Thread ID: backend thread/resume will verify cwd, idle status, and provider allowlist.
    return {"thread_id": target, "continued_from": None, "prior_provider": requested_provider}


def make_run_id():
    return f"RUN-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:6]}"


def reconcile_run(ws, run_id, *, codex_cmd=None, timeout=30, cancel=None,
                  on_state_sync=None, inbox_target=None):
    """Read-only reconciliation for a pending/ambiguous Run; never replays turns."""
    state = load_run(ws, run_id)
    if state.get("host") and state["host"] != socket.gethostname():
        raise WorkflowError("Recovery requires the original host to verify the worker process.")
    if pid_alive(state.get("child_pid"), process_group=os.name != "nt"):
        raise WorkflowError("The saved worker/process group is still alive. No replacement was started.")

    artifact_dir = Path(state["artifact_dir"])
    inbox_dir = Path(state["inbox_dir"])

    def _save(event=None):
        save_run(ws, state, event)
        if on_state_sync:
            on_state_sync(state, event)

    if inbox_target is None:
        import inbox as _inbox
        inbox_target = _inbox.InboxTarget(
            inbox_dir=inbox_dir,
            owner_id=state["run_id"],
            load_state=lambda: load_run(ws, run_id),
            save_state=lambda st, ev=None: _save(ev),
            round_number=state.get("round", 1),
        )

    if state.get("turn_pending"):
        from backends import codex_app_server
        codex_app_server.reconcile_run(
            ws=ws, state=state, save_fn=_save,
            artifact_dir=artifact_dir, inbox_target=inbox_target,
            codex_cmd=codex_cmd, timeout=timeout, cancel=cancel,
        )
    if state.get("inbox_window"):
        import inbox as _inbox
        _inbox.recover_window(inbox_target, state)

    if state.get("status") in {"running", "unknown"}:
        term = state.get("last_turn_status")
        if term == "completed":
            state["status"] = "completed"
        elif term == "interrupted":
            state["status"] = "interrupted"
        elif term == "failed":
            state["status"] = "failed"
        state["child_pid"] = None
        if not state.get("ended_at") and state["status"] in {"completed", "failed", "interrupted"}:
            state["ended_at"] = now()
        _save("run_reconciled")
    return state


def execute_run(
    ws,
    prompt,
    *,
    run_id=None,
    artifact_dir=None,
    inbox_dir=None,
    inbox_target=None,
    resume=None,
    session_id=None,
    continued_from=None,
    prior_provider=None,
    codex=None,
    tier=None,
    backend_override=None,
    sandbox="workspace-write",
    output_schema=None,
    codex_cmd=None,
    timeout=900,
    cancel=None,
    on_progress=None,
    on_event=None,
    on_state_sync=None,
    owner_kind="lightweight",
    ticket_id=None,
    round_number=1,
    allow_strict=False,
    already_locked=False,
    update_latest=True,
    supplement_prompt_builder=None,
    queue_turns=0,
):
    """Single shared execution core used by both lightweight CLI and Strict Controller."""
    validate_sandbox(sandbox)
    if not isinstance(prompt, str) or not prompt.strip():
        raise WorkflowError("Prompt must be nonempty.")
    cancel = cancel or threading.Event()
    _persist_workspace_meta(ws)

    from config import load as load_config
    from routing import policy, select, verify_provider

    cfg = load_config(ws.state_dir)
    tier_settings = select(ws.state_dir, tier) if tier is not None else select(ws.state_dir)
    requested = codex_settings(tier_settings, cfg["codex"], codex)
    if backend_override is not None:
        if backend_override not in ("auto", "app-server", "exec"):
            raise WorkflowError("backend must be auto, app-server, or exec.")
        backend = backend_override
    else:
        backend = cfg.get("backend", "exec" if codex_cmd else "auto")

    if resume is not None:
        resolved = resolve_resume(ws, resume, requested_provider=requested.get("model_provider"))
        session_id = resolved["thread_id"]
        continued_from = resolved["continued_from"]
        if resolved["prior_provider"] and not prior_provider:
            prior_provider = resolved["prior_provider"]

    if prior_provider:
        if requested.get("model_provider") not in (None, prior_provider):
            raise WorkflowError("Cannot change provider inside an existing Session.")
        requested["model_provider"] = prior_provider

    if requested.get("model_provider") or (backend == "exec" and policy(ws.state_dir)["allowed_providers"]):
        verify_provider(ws.state_dir, requested.get("model_provider"))

    launcher = codex_cmd or resolve_codex()
    run_id = _valid_run_id(run_id) if run_id else make_run_id()
    rdir = run_dir_for(ws, run_id)
    artifact_dir = Path(artifact_dir) if artifact_dir else rdir
    inbox_dir = Path(inbox_dir) if inbox_dir else (rdir / "inbox")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    rdir.mkdir(parents=True, exist_ok=True)

    started_monotonic = time.monotonic()
    state = {
        "schema_version": 1,
        "run_id": run_id,
        "owner_kind": owner_kind,
        "ticket_id": ticket_id,
        "round": round_number,
        "thread_id": session_id,
        "continued_from": continued_from,
        "workspace_key": ws.workspace_key,
        "cwd": str(ws.root_path),
        "is_git": ws.is_git,
        "codex_home": current_codex_home(),
        "host": socket.gethostname(),
        "backend": None,
        "model_provider": prior_provider,
        "codex_requested": requested,
        "sandbox": sandbox,
        "status": "running",
        "turn_pending": False,
        "active_turn_id": None,
        "last_turn_id": None,
        "last_turn_status": None,
        "inbox_window": None,
        "queue_turns": queue_turns,
        "child_pid": None,
        "last_child_pid": None,
        "artifact_dir": str(artifact_dir),
        "inbox_dir": str(inbox_dir),
        "result_path": str(artifact_dir / "last-message.md"),
        "usage": {},
        "error": None,
        "started_at": now(),
        "ended_at": None,
        "elapsed_seconds": 0,
    }

    def _save(event=None):
        state["elapsed_seconds"] = round(time.monotonic() - started_monotonic, 2)
        save_run(ws, state, event)
        if on_state_sync:
            on_state_sync(state, event)

    if inbox_target is None:
        import inbox as _inbox
        inbox_target = _inbox.InboxTarget(
            inbox_dir=inbox_dir,
            owner_id=run_id,
            load_state=lambda: load_run(ws, run_id),
            save_state=lambda st, ev=None: _save(ev),
            round_number=round_number,
        )

    schema_path = None
    if output_schema is not None:
        schema_path = artifact_dir / "worker-schema.json"
        atomic_json(schema_path, output_schema)

    with acquire_writer_lease(ws, run_id, sandbox=sandbox, allow_strict=allow_strict, already_locked=already_locked):
        (artifact_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        _save("run_started")
        if update_latest:
            atomic_json(ws.state_dir / "latest-run.json", {"run_id": run_id, "updated_at": now()})

        summary = EventSummary()

        def on_line(raw):
            event = summary.feed(raw)
            if event and event.get("type") == "thread.started":
                tid = event.get("thread_id")
                if not isinstance(tid, str) or not tid:
                    raise WorkflowError("Codex emitted an invalid session ID.")
                if state["thread_id"] and state["thread_id"] != tid:
                    raise WorkflowError("Resumed Codex returned a different Session ID. Original mapping retained.")
                state["thread_id"] = tid
                _save("session_bound")
            if event and event.get("type") in {"item.started", "item.completed"}:
                item = event.get("item") or {}
                if on_progress:
                    if item.get("type") == "command_execution":
                        on_progress({"kind": "command", "completed": event["type"] == "item.completed",
                                     "command": item.get("command")})
                    elif item.get("type") == "file_change" and event["type"] == "item.completed":
                        on_progress({"kind": "files", "paths": [c.get("path", "") for c in item.get("changes", [])]})
            if on_event and event:
                on_event(event)

        if on_progress:
            on_progress({"kind": "stage", "stage": "worker", "round": round_number})

        try:
            from backends import codex_app_server, codex_exec
            if backend == "exec":
                rc = codex_exec.run(
                    ws=ws, state=state, save_fn=_save, launcher=launcher,
                    requested=requested, artifact_dir=artifact_dir, prompt=prompt,
                    on_line=on_line, sandbox=sandbox, output_schema_path=schema_path,
                    timeout=timeout, cancel=cancel,
                )
            else:
                try:
                    rc = codex_app_server.run(
                        ws=ws, state=state, save_fn=_save, launcher=launcher,
                        requested=requested, artifact_dir=artifact_dir,
                        inbox_target=inbox_target, prompt=prompt, on_line=on_line,
                        sandbox=sandbox, output_schema=output_schema,
                        timeout=timeout, cancel=cancel,
                        supplement_prompt_builder=supplement_prompt_builder,
                    )
                except codex_app_server.StartupUnavailable as exc:
                    if backend != "auto" or state.get("turn_pending"):
                        raise
                    if policy(ws.state_dir)["allowed_providers"]:
                        verify_provider(ws.state_dir, requested.get("model_provider"))
                    atomic_json(artifact_dir / "backend-fallback.json", {
                        "reason": str(exc), "from": "app-server", "to": "exec", "before_turn": True,
                    })
                    rc = codex_exec.run(
                        ws=ws, state=state, save_fn=_save, launcher=launcher,
                        requested=requested, artifact_dir=artifact_dir, prompt=prompt,
                        on_line=on_line, sandbox=sandbox, output_schema_path=schema_path,
                        timeout=timeout, cancel=cancel,
                    )

            summary.data["exit_code"] = rc
            atomic_json(artifact_dir / "summary.json", summary.data)
            state["usage"] = summary.data.get("usage") or {}
            clean_ok = (
                rc == 0 and summary.data["completed"] and not summary.data["failed"]
                and not summary.data["invalid_lines"]
                and bool(state["thread_id"])
                and summary.data["session_id"] == state["thread_id"]
            )
            state["status"] = "completed" if clean_ok else "failed"
            if not clean_ok:
                state["error"] = "Codex execution did not complete cleanly; inspect summary.json and stderr.log."
            state["ended_at"] = now()
            _save("run_completed" if clean_ok else "run_failed")
            return state, summary.data
        except (Exception, KeyboardInterrupt) as exc:
            saved_summary = artifact_dir / "summary.json"
            if (state.get("backend") == "app-server" and not state.get("turn_pending")
                    and state.get("last_turn_status") == "completed" and summary.data["completed"]
                    and not summary.data["failed"] and not summary.data["invalid_lines"]
                    and summary.data["session_id"] == state["thread_id"] and saved_summary.exists()):
                done = read_json(saved_summary)
                if (done.get("exit_code") == 0 and done.get("completed") and not done.get("failed")
                        and not done.get("invalid_lines") and done.get("session_id") == state["thread_id"]):
                    summary.data["exit_code"] = 0
            summary.data["controller_error"] = str(exc) or type(exc).__name__
            atomic_json(saved_summary, summary.data)
            state["usage"] = summary.data.get("usage") or state.get("usage") or {}
            state["error"] = summary.data["controller_error"]
            if state.get("turn_pending"):
                state["status"] = "unknown"
            elif isinstance(exc, (Interrupted, KeyboardInterrupt)):
                state["status"] = "interrupted"
            else:
                state["status"] = "failed"
            state["ended_at"] = now()
            _save("run_aborted")
            return state, summary.data


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "_child-gate":
        raise SystemExit(launch_gate(json.loads(sys.argv[2])))
