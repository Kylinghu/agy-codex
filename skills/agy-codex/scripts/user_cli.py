#!/usr/bin/env python3
"""Lightweight CLI for AGY Codex: run, status, inbox, models, doctor (Git & non-Git)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import subprocess
import sys
import threading

_scripts_dir = str(Path(__file__).resolve().parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

import inbox as inbox_mod
from progress import CliProgress
import runtime
from runtime import (
    SANDBOX_MODES,
    WorkflowError,
    execute_run,
    latest_run,
    list_runs,
    load_run,
    read_json,
    reconcile_run,
    resolve_codex,
    resolve_workspace,
)


def _add_workspace_arg(parser):
    parser.add_argument("--cwd", "--repo", dest="cwd", default=None,
                        help="Target workspace directory (Git or non-Git; defaults to current directory)")


def _read_prompt(args):
    if args.prompt_file:
        return Path(args.prompt_file).expanduser().read_text(encoding="utf-8")
    if args.prompt == "-":
        return sys.stdin.read()
    if args.prompt:
        return args.prompt
    if args.prompt_text:
        return args.prompt_text
    if not sys.stdin.isatty():
        text = sys.stdin.read()
        if text.strip():
            return text
    raise WorkflowError("Provide a prompt via positional text, --prompt, --prompt-file, or stdin.")


def _read_result_excerpt(path_str, max_chars=2000):
    p = Path(path_str)
    if not p.is_file():
        return None
    text = p.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... [truncated; full output in {p}]"


def _run_summary(state, summary_data=None):
    result_path = state.get("result_path")
    has_result = bool(result_path and Path(result_path).is_file())
    out = {
        "kind": "run",
        "run_id": state["run_id"],
        "thread_id": state.get("thread_id"),
        "continued_from": state.get("continued_from"),
        "workspace": state.get("cwd"),
        "is_git": state.get("is_git"),
        "status": state.get("status"),
        "backend": state.get("backend"),
        "model_provider": state.get("model_provider"),
        "codex_requested": state.get("codex_requested"),
        "sandbox": state.get("sandbox"),
        "turn_pending": bool(state.get("turn_pending")),
        "active_turn_id": state.get("active_turn_id"),
        "inbox_window": state.get("inbox_window"),
        "elapsed_seconds": state.get("elapsed_seconds"),
        "usage": state.get("usage") or {},
        "result_path": result_path if has_result else None,
        "artifact_dir": state.get("artifact_dir"),
        "error": state.get("error"),
        "started_at": state.get("started_at"),
        "ended_at": state.get("ended_at"),
    }
    if summary_data is not None:
        out["changed_files"] = summary_data.get("changed_files", [])
        out["commands_count"] = len(summary_data.get("commands", []))
    progress_path = Path(state["artifact_dir"]) / "progress.json" if state.get("artifact_dir") else None
    if progress_path and progress_path.is_file():
        try:
            out["progress"] = read_json(progress_path)
        except (OSError, ValueError):
            pass
    if has_result:
        out["result_excerpt"] = _read_result_excerpt(result_path)
    return out


def _strict_status_summary(ws, *, ticket=None, verify=False):
    if not ws.is_git:
        return None
    from controller import Controller
    from plans import load_plan, plan_report
    from product import active_id, report
    if not (ws.state_dir / "active.json").exists() and not (ws.state_dir / "active-plan.json").exists() and not ticket:
        return None
    c = Controller(ws.root_path)
    tid = active_id(c, ticket)
    plan = load_plan(c) if ticket is None else None
    if plan:
        if not verify:
            # Lightweight offline summary without hashing the full repository.
            tasks = []
            done = 0
            for entry in plan["tasks"]:
                tp = c.ticket_dir(entry["ticket_id"]) / "ticket.json"
                st = c.load(entry["ticket_id"]) if tp.is_file() else None
                status = st["status"] if st else "PENDING"
                if status == "PASS":
                    done += 1
                tasks.append({"key": entry["key"], "ticket_id": entry["ticket_id"],
                              "title": entry["task"]["title"], "status": status, "round": st["round"] if st else 0})
            return {"kind": "plan", "plan_id": plan["id"], "title": plan["title"],
                    "stopped": bool(plan.get("stopped")), "completed": done, "total": len(tasks),
                    "tasks": tasks, "snapshot_verified": False}
        return plan_report(c, plan)
    if tid:
        st = c.load(tid)
        if not verify:
            return {"kind": "ticket", "ticket_id": st["id"], "title": st["spec"]["title"],
                    "status": st["status"], "round": st["round"], "max_rounds": st["max_rounds"],
                    "session_id": st.get("session_id"), "inbox_window": st.get("inbox_window"),
                    "resolution_allowed": bool(st.get("resolution_allowed")),
                    "recoverable": bool(st.get("recoverable_interruption")),
                    "reason": st.get("reason"), "snapshot_verified": False}
        return report(c, st)
    return None


def _cmd_run(args, ws):
    prompt = _read_prompt(args)
    sandbox = "read-only" if args.read_only else args.sandbox
    codex_override = {}
    if args.model:
        codex_override["model"] = args.model
    if args.effort:
        codex_override["reasoning_effort"] = args.effort
    if args.provider:
        codex_override["model_provider"] = args.provider
    output_schema = read_json(args.schema) if args.schema else None
    launcher = resolve_codex(args.codex) if args.codex else None

    cancel = threading.Event()
    def _handle_signal(signum, frame):
        cancel.set()

    prev_int = signal.signal(signal.SIGINT, _handle_signal)
    prev_term = None
    if hasattr(signal, "SIGTERM"):
        try:
            prev_term = signal.signal(signal.SIGTERM, _handle_signal)
        except (OSError, ValueError):
            prev_term = None

    run_id = runtime.make_run_id()
    progress_path = runtime.run_dir_for(ws, run_id) / "progress.json"
    reporter = CliProgress(progress_path, run_id, stream=None if args.quiet else sys.stderr)

    try:
        state, summary_data = execute_run(
            ws, prompt,
            run_id=run_id,
            resume=args.resume,
            codex=codex_override or None,
            tier=args.tier,
            backend_override=args.backend,
            sandbox=sandbox,
            output_schema=output_schema,
            codex_cmd=launcher,
            timeout=args.timeout,
            cancel=cancel,
            on_progress=reporter.emit,
            owner_kind="lightweight",
        )
    finally:
        signal.signal(signal.SIGINT, prev_int)
        if prev_term is not None:
            signal.signal(signal.SIGTERM, prev_term)

    reporter.finish(state["status"])
    out = _run_summary(state, summary_data)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"Run {out['run_id']} ({out['status']}) | Thread: {out['thread_id'] or 'none'} | "
              f"Backend: {out['backend'] or 'none'} | {out['elapsed_seconds']}s")
        if out.get("changed_files"):
            print("Changed files: " + ", ".join(out["changed_files"]))
        if out.get("usage"):
            print("Usage: " + json.dumps(out["usage"], ensure_ascii=False))
        if out.get("result_path"):
            print("Result file: " + out["result_path"])
        if out.get("error"):
            print("Error: " + out["error"])
        if out.get("result_excerpt"):
            print("\n" + out["result_excerpt"])
    return 0 if state["status"] == "completed" else 2


def _cmd_status(args, ws):
    if args.reconcile:
        rid = args.run or (latest_run(ws) or {}).get("run_id")
        if not rid:
            raise WorkflowError("No saved Run to reconcile in this workspace.")
        launcher = resolve_codex(args.codex) if args.codex else None
        state = reconcile_run(ws, rid, codex_cmd=launcher)
        out = _run_summary(state)
        print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else
              f"Reconciled {out['run_id']}: {out['status']} (Thread {out['thread_id']})")
        return 0 if state["status"] == "completed" else 2

    if args.ticket:
        strict = _strict_status_summary(ws, ticket=args.ticket, verify=args.verify)
        if strict is None:
            raise WorkflowError("Strict Ticket status requires a Git repository.")
        print(json.dumps(strict, ensure_ascii=False, indent=2) if args.json else
              strict.get("message") or f"{strict.get('ticket_id')}: {strict.get('status')}")
        return 0

    if args.run:
        state = load_run(ws, args.run)
        out = _run_summary(state)
    else:
        latest = latest_run(ws)
        recent = [{
            "run_id": r["run_id"], "thread_id": r.get("thread_id"),
            "status": r.get("status"), "continued_from": r.get("continued_from"),
            "sandbox": r.get("sandbox"), "started_at": r.get("started_at"),
            "ended_at": r.get("ended_at"), "result_path": r.get("result_path"),
        } for r in list_runs(ws, limit=args.limit)]
        strict = _strict_status_summary(ws, verify=args.verify)
        if latest is None and strict is not None and args.verify:
            out = strict
        else:
            out = {
                "workspace": str(ws.root_path),
                "is_git": ws.is_git,
                "state_directory": str(ws.state_dir),
                "latest_run": _run_summary(latest) if latest else None,
                "recent_runs": recent,
                "strict": strict,
            }

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        if out.get("kind") == "run":
            print(f"Run {out['run_id']} ({out['status']}) | Thread: {out['thread_id'] or 'none'}")
            if out.get("result_path"):
                print("Result file: " + out["result_path"])
        elif out.get("message"):
            print(out["message"])
            if out.get("summary"):
                print(out["summary"])
            if out.get("next_action"):
                print(out["next_action"])
        else:
            lr = out.get("latest_run")
            if lr:
                print(f"Workspace: {out['workspace']} ({'git' if out['is_git'] else 'non-git'})")
                print(f"Latest run: {lr['run_id']} ({lr['status']}) | Thread: {lr['thread_id'] or 'none'} | {lr['elapsed_seconds']}s")
                if lr.get("result_path"):
                    print("Result file: " + lr["result_path"])
                prog = lr.get("progress")
                if prog and prog.get("running"):
                    print(f"Live progress ({prog.get('updated_at')}): {prog.get('stage')} | "
                          f"cmds={prog.get('commands_completed', 0)} files={prog.get('changed_file_count', 0)}")
            else:
                print(f"Workspace: {out['workspace']} ({'git' if out['is_git'] else 'non-git'}) — no runs recorded yet.")
            if out.get("strict"):
                st = out["strict"]
                label = st.get("plan_id") or st.get("ticket_id")
                print(f"Active Strict: {label} ({st.get('status', 'active')})")
    return 0


def _resolve_inbox_target(ws, args):
    if args.ticket:
        if not ws.is_git:
            raise WorkflowError("--ticket requires a Git workspace.")
        from controller import Controller
        c = Controller(ws.root_path)
        return c, args.ticket

    if args.run:
        st = load_run(ws, args.run)
        return inbox_mod.InboxTarget(
            inbox_dir=Path(st["inbox_dir"]),
            owner_id=st["ticket_id"] if st.get("owner_kind") == "strict" and st.get("ticket_id") else st["run_id"],
            load_state=lambda: load_run(ws, args.run),
            save_state=lambda s, ev=None: runtime.save_run(ws, s, ev),
            round_number=st.get("round", 1),
        ), None

    # Prefer any running Run with an open inbox window.
    runs = list_runs(ws, limit=10)
    active_run = next((r for r in runs if r.get("status") == "running" and r.get("inbox_window")), None)
    if active_run is not None:
        rid = active_run["run_id"]
        return inbox_mod.InboxTarget(
            inbox_dir=Path(active_run["inbox_dir"]),
            owner_id=active_run["ticket_id"] if active_run.get("owner_kind") == "strict" and active_run.get("ticket_id") else rid,
            load_state=lambda: load_run(ws, rid),
            save_state=lambda s, ev=None: runtime.save_run(ws, s, ev),
            round_number=active_run.get("round", 1),
        ), None

    # Fallback to active Strict Ticket if present (for backward compatibility with status/history).
    if ws.is_git and (ws.state_dir / "active.json").exists():
        from controller import Controller
        from product import active_id
        c = Controller(ws.root_path)
        tid = active_id(c)
        if tid:
            return c, tid

    if runs:
        rid = runs[0]["run_id"]
        return inbox_mod.InboxTarget(
            inbox_dir=Path(runs[0]["inbox_dir"]),
            owner_id=rid,
            load_state=lambda: load_run(ws, rid),
            save_state=lambda s, ev=None: runtime.save_run(ws, s, ev),
            round_number=runs[0].get("round", 1),
        ), None
    raise WorkflowError("No active Run or Ticket in this workspace.")


def _cmd_inbox(args, ws):
    target, legacy_ticket = _resolve_inbox_target(ws, args)
    if args.operation == "status":
        result = inbox_mod.history(target, legacy_ticket) if legacy_ticket else inbox_mod.history(target)
    else:
        if legacy_ticket:
            result = inbox_mod.publish(target, legacy_ticket, args.operation, args.text)
        else:
            result = inbox_mod.publish(target, args.operation, args.text)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _cmd_models(args, ws):
    from routing import probe
    launcher = resolve_codex(args.codex) if args.codex else None
    print(json.dumps(probe(ws, codex_cmd=launcher, refresh=args.refresh), ensure_ascii=False, indent=2))
    return 0


def _cmd_doctor(args):
    from controller import doctor
    cwd = args.cwd or "."
    launcher = resolve_codex(args.codex) if args.codex else None
    result = doctor(cwd, launcher, require_git=args.require_git, check_windows_sandbox=True)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("执行环境检查通过。" if result["static_checks_passed"] else "执行环境需要处理：")
        print("Python: " + result["python_executable"])
        print("Git: " + (result["git_executable"] or "未找到"))
        if result.get("windows_sandbox_readiness"):
            print("Windows Sandbox: " + str(result["windows_sandbox_readiness"]))
        for key, passed in result["checks"].items():
            note = "（非 Git 目录普通模式非必需）" if (not args.require_git and key.startswith("git_") and not passed) else ""
            print(f"  {'通过' if passed else '未通过'}  {key}{note}")
        if result.get("git_error") and args.require_git:
            print("Git 诊断：" + result["git_error"])
    return 0 if result["static_checks_passed"] else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", "--repo", dest="top_cwd", default=None,
                        help="Target workspace directory (Git or non-Git)")
    parser.add_argument("--codex", dest="top_codex", default=None,
                        help="Optional explicit Codex launcher path")
    commands = parser.add_subparsers(dest="action", required=True)

    run_p = commands.add_parser("run", help="Run Codex foreground task or incremental resume")
    _add_workspace_arg(run_p)
    run_p.add_argument("prompt", nargs="?", default=None, help="Prompt text, or '-' to read stdin")
    run_p.add_argument("--prompt", dest="prompt_text", default=None)
    run_p.add_argument("--prompt-file", default=None)
    run_p.add_argument("--resume", default=None, help="Resume RUN_ID, native THREAD_ID, or 'last'")
    run_p.add_argument("--model", default=None)
    run_p.add_argument("--effort", "--reasoning-effort", dest="effort", default=None)
    run_p.add_argument("--provider", default=None)
    run_p.add_argument("--tier", default=None)
    run_p.add_argument("--sandbox", choices=SANDBOX_MODES, default="workspace-write")
    run_p.add_argument("--read-only", action="store_true", help="Run with sandbox=read-only")
    run_p.add_argument("--schema", default=None, help="Optional JSON Schema file path")
    run_p.add_argument("--backend", choices=("auto", "app-server", "exec"), default=None)
    run_p.add_argument("--timeout", type=int, default=900)
    run_p.add_argument("--codex", default=None)
    run_p.add_argument("--quiet", action="store_true", help="Suppress stderr live progress lines")
    run_p.add_argument("--json", action="store_true")

    status_p = commands.add_parser("status", help="Offline workspace status and recent runs")
    _add_workspace_arg(status_p)
    status_p.add_argument("--run", default=None)
    status_p.add_argument("--ticket", default=None)
    status_p.add_argument("--limit", type=int, default=10)
    status_p.add_argument("--verify", action="store_true", help="Verify Strict Git snapshot hash")
    status_p.add_argument("--reconcile", action="store_true", help="Reconcile a pending/unknown Run via native Thread history")
    status_p.add_argument("--codex", default=None)
    status_p.add_argument("--json", action="store_true")

    inbox_p = commands.add_parser("inbox", help="Send steer/queue/interrupt or inspect live delivery")
    _add_workspace_arg(inbox_p)
    inbox_p.add_argument("operation", choices=("queue", "steer", "interrupt", "status"))
    inbox_p.add_argument("text", nargs="?", default="")
    inbox_p.add_argument("--run", default=None)
    inbox_p.add_argument("--ticket", default=None)
    inbox_p.add_argument("--json", action="store_true")

    models_p = commands.add_parser("models", help="Discover native Codex models and rate windows")
    _add_workspace_arg(models_p)
    models_p.add_argument("--refresh", action="store_true", help="Bypass TTL cache and query live RPCs")
    models_p.add_argument("--codex", default=None)
    models_p.add_argument("--json", action="store_true")

    doc_p = commands.add_parser("doctor", help="Environment readiness check (Git & non-Git)")
    _add_workspace_arg(doc_p)
    doc_p.add_argument("--require-git", action="store_true", help="Fail static checks when outside a Git repo")
    doc_p.add_argument("--codex", default=None)
    doc_p.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    args.cwd = getattr(args, "cwd", None) or args.top_cwd or "."
    if getattr(args, "codex", None) is None:
        args.codex = args.top_codex

    try:
        if args.action == "doctor":
            return _cmd_doctor(args)
        ws = resolve_workspace(args.cwd)
        if args.action == "run":
            return _cmd_run(args, ws)
        if args.action == "status":
            return _cmd_status(args, ws)
        if args.action == "inbox":
            return _cmd_inbox(args, ws)
        if args.action == "models":
            return _cmd_models(args, ws)
        raise WorkflowError(f"Unsupported action: {args.action}")
    except (WorkflowError, OSError, ValueError, subprocess.SubprocessError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False))
        else:
            print("操作未完成：" + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
