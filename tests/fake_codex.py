#!/usr/bin/env python3
"""Deterministic *test double*, never a real model or authentication client.

Runs in a disposable Git repository. Its control file and invocation ledger live
under .git, so they cannot accidentally appear as implementation changes.
"""
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid


def emit(event):
    print(json.dumps(event), flush=True)


def main():
    argv = sys.argv[1:]
    if argv[:5] != ["-a", "never", "-s", "workspace-write", "exec"]:
        print("Unexpected CLI contract: " + repr(argv), file=sys.stderr)
        return 64
    rest = argv[5:]
    resumed = bool(rest and rest[0] == "resume")
    if resumed:
        rest = rest[1:]
    if len(rest) != (7 if resumed else 6) or rest[:2] != ["--json", "-o"] or rest[3] != "--output-schema" or rest[-1] != "-":
        print("Unexpected exec arguments: " + repr(rest), file=sys.stderr)
        return 64
    output = Path(rest[2])
    schema = json.loads(Path(rest[4]).read_text())
    if schema["properties"]["outcome"]["enum"] != ["done", "decision_required", "blocked"]:
        return 64
    requested_session = rest[5] if resumed else None
    prompt = sys.stdin.read()
    if not prompt.strip():
        print("A prompt on stdin is required", file=sys.stderr)
        return 64

    repo = Path.cwd()
    gitdir = repo / ".git"
    control_path = gitdir / "fake-codex-control.json"
    control = json.loads(control_path.read_text()) if control_path.exists() else {}
    mode = control.get("mode", "success")
    ledger = gitdir / "fake-codex-calls.jsonl"
    previous = ledger.read_text().splitlines() if ledger.exists() else []
    count = len(previous) + 1
    original_session = requested_session or str(uuid.uuid5(uuid.NAMESPACE_URL, f"{repo}:{count}"))
    returned_session = str(uuid.uuid4()) if mode == "mismatch-resume" and resumed else original_session
    with ledger.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"argv": argv, "resume": resumed,
                                 "requested_session": requested_session,
                                 "returned_session": returned_session,
                                 "prompt": prompt, "call": count}) + "\n")

    if control.get("startup_delay"):
        time.sleep(float(control["startup_delay"]))
    if mode != "missing-thread":
        emit({"type": "thread.started", "thread_id": returned_session})
    emit({"type": "turn.started"})
    if mode == "silent-wait":
        deadline = time.monotonic() + 15
        while not (gitdir / "fake-progress-release").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
    if mode == "hold":
        # A bounded wait lets a separate test process attempt the same worktree.
        time.sleep(float(control.get("seconds", 0.8)))
    if mode == "orphan-descendant":
        ready = gitdir / "fake-descendant-ready"
        escaped = gitdir / "fake-descendant-wrote"
        descendant = subprocess.Popen(
            [sys.executable, "-c",
             "import signal,sys,time; from pathlib import Path; "
             "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
             "Path(sys.argv[1]).write_text('ready'); time.sleep(0.7); "
             "Path(sys.argv[2]).write_text('escaped'); "
             "Path('value.py').write_text('VALUE = 99999\\n')",
             str(ready), str(escaped)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        (gitdir / "fake-descendant-pid").write_text(str(descendant.pid))
        deadline = time.monotonic() + 2
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        if not ready.exists():
            print("Descendant fixture failed to initialize", file=sys.stderr)
            return 65
    if mode == "add-delete":
        (repo / "value.py").unlink()
        (repo / "created.txt").write_text("new untracked evidence\n", encoding="utf-8")
        changes = [{"path": "value.py", "kind": "delete"},
                   {"path": "created.txt", "kind": "add"}]
    else:
        (repo / "value.py").write_text(f"VALUE = {count}\n", encoding="utf-8")
        changes = [{"path": "value.py", "kind": "update"}]
    if control.get("update_plan"):
        (repo / "PLAN.md").write_text(f"# Agreed fixture plan\nCompleted invocation {count}.\n", encoding="utf-8")
        changes.append({"path": "PLAN.md", "kind": "update"})
    emit({"type": "item.completed", "item": {"id": "item_1", "type": "file_change",
                                               "status": "completed", "changes": changes}})
    emit({"type": "item.completed", "item": {"id": "item_2", "type": "command_execution",
                                               "command": "test-double internal check",
                                               "exit_code": 0, "status": "completed"}})
    if mode == "burst-wait":
        emit({"type": "item.completed", "item": {"type": "reasoning", "text": "DO_NOT_DISPLAY_REASONING"}})
        emit({"type": "item.completed", "item": {"type": "agent_message", "text": "DO_NOT_DISPLAY_AGENT_TEXT"}})
        for index in range(100):
            emit({"type": "item.completed", "item": {"type": "command_execution", "id": f"burst-{index}",
                  "command": "DO_NOT_DISPLAY_COMMAND", "aggregated_output": "DO_NOT_DISPLAY_STDOUT", "exit_code": 0}})
    if mode in {"progress-wait", "burst-wait"}:
        (gitdir / "fake-progress-ready").write_text("ready")
        deadline = time.monotonic() + 15
        while not (gitdir / "fake-progress-release").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
    outcome = {"outcome": "done", "summary": f"TEST DOUBLE ONLY. Updated fixture in invocation {count}.",
               "changes": ["Updated value fixture"], "checks": ["Test double internal check"], "issue": None}
    if mode in {"decision", "permission", "environment"}:
        kind = control.get("kind", "engineering_choice" if mode == "decision" else mode)
        outcome.update(outcome="decision_required" if mode == "decision" else "blocked",
                       issue={"kind": kind, "detail": "Injected concrete issue", "evidence": "Explicit test fixture evidence",
                              "question": "Choose a fixture outcome", "recommendation": "Use the existing fixture convention"})
    output.write_text("bad final JSON" if mode == "invalid-report" else json.dumps(outcome), encoding="utf-8")
    if mode == "malformed":
        print("this is deliberately not JSON", flush=True)
    if mode != "missing-completed":
        emit({"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 8}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
