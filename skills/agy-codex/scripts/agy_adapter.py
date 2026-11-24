"""AGY workflow: serial tasks or an agreed plan, with bounded live progress."""
from __future__ import annotations

import asyncio
from pathlib import Path

from controller import Controller, REVIEW_SCHEMA, WorkflowError, atomic_json, codex_settings, doctor, worktree_lock
import plans
from progress import run_controller, validate_mode
from product import active_id, report, review_input, reviewer_prompt, start_task


async def _run_ticket(c, ticket_id, options, agent_fn, phase_fn, log_fn, preflight, *, resolve=False):
    state = c.load(ticket_id)
    progress_mode = options.get("progress", "stages")
    if resolve:
        resolution = options.get("resolution")
        if not isinstance(resolution, dict):
            raise WorkflowError("Resolve needs the recorded answer, source, and observed evidence where applicable.")
        state = c.resolve(ticket_id, resolution.get("answer"), resolution.get("source"), resolution.get("evidence", ""), resolution.get("round"))

    if state["status"] == "RUNNING" or state.get("recoverable_interruption"):
        phase_fn("恢复当前任务")
        state = await run_controller(c, c.recover, ticket_id, phase_fn, log_fn, progress_mode=progress_mode)
    phase_fn("检查执行环境")
    if preflight and state["status"] in {"READY", "REVISE"}:
        checks = doctor(c.repo, c.codex_cmd)
        atomic_json(c.ticket_dir(ticket_id)/"doctor.json", checks)
        if not checks["static_checks_passed"]:
            failed = [name for name, ok in checks["checks"].items() if not ok]
            message = "执行环境尚未就绪：" + ", ".join(failed) + "。任务已保存，处理后说“继续”即可。"
            log_fn(message)
            return {"ticket_id": ticket_id, "status": "BLOCKED", "message": message,
                    "doctor": checks, "next_action": "处理未通过的环境检查后继续当前任务。"}
    reviewer = None
    while True:
        state = c.load(ticket_id)
        if state["status"] in {"PASS", "ESCALATE"}:
            result = report(c, state)
            log_fn(result["message"])
            return result
        if state["status"] in {"READY", "REVISE"}:
            phase_fn(f"Codex {'修复' if state['status'] == 'REVISE' else '实现'}：第 {state['round']+1}/{state['max_rounds']} 轮")
            state = await run_controller(c, c.run, ticket_id, phase_fn, log_fn, progress_mode=progress_mode, **({"codex": options["codex"]} if options.get("codex") else {}))
            if state["status"] != "REVIEW":
                continue
        if state["status"] != "REVIEW":
            raise WorkflowError(f"Unexpected task state: {state['status']}")
        phase_fn("AGY 复审：需求符合性与工程规范")
        try:
            packet = c.packet(ticket_id)
        except WorkflowError as exc:
            message = "复审证据与当前文件不一致。先核对外部改动，再刷新验收证据；没有重复启动 Codex。"
            log_fn(message)
            return {"ticket_id": ticket_id, "status": "REVIEW", "message": message,
                    "review_error": str(exc), "snapshot_current": False,
                    "next_action": "核对改动范围后刷新当前任务的验收与复审证据。"}
        fresh = reviewer is None
        if fresh:
            reviewer = agent_fn(role="AGY read-only reviewer", type_name="research", workspace="inherit")
        data = review_input(c, state, packet, fresh)
        prompt = reviewer_prompt(data)
        directory = c._round_dir(state)
        atomic_json(directory/"review-input.json", data)
        (directory/"review-prompt.txt").write_text(prompt, encoding="utf-8")
        error = None
        for attempt in (1, 2):
            try:
                review = await asyncio.wait_for(reviewer(prompt, schema=REVIEW_SCHEMA),
                                                timeout=int(options.get("review_timeout", 900)))
                state = c.verdict(ticket_id, review, source="agy-research")
                error = None
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = exc
                atomic_json(directory/f"review-attempt-{attempt}-error.json", {"error": str(exc)})
                # Check for an external edit before retrying a read-only response.
                try:
                    c.packet(ticket_id)
                except WorkflowError:
                    break
                transient = isinstance(exc, (ValueError, WorkflowError, TimeoutError, ConnectionError)) or (
                    isinstance(exc, RuntimeError) and any(word in str(exc).lower() for word in
                                                        ("schema", "json", "format", "temporar", "rate limit", "timeout", "timed out")))
                if not isinstance(exc, WorkflowError) and any(word in str(exc).lower() for word in ("permission", "denied", "unauthorized", "forbidden")):
                    transient = False
                if attempt == 2 or not transient:
                    break
                log_fn("复审回复需要修正，正在原只读会话重试一次；不会重复实现。")
                prompt = ("Correct only the review response in this same read-only session. "
                          "Reuse the evidence already supplied; do not edit or rerun tests. "
                          f"The validator reported: {exc}\n"
                          f"Exact ticket_id={ticket_id}, round={state['round']}, snapshot_sha256={packet['snapshot_sha256']}. "
                          "Return the required schema and keep any genuine blocker visible.")
                (directory/"review-retry-prompt.txt").write_text(prompt, encoding="utf-8")
        if error is not None:
            message = "复审尚未完成，已保留当前实现和证据。处理复审问题后说“继续”，不会重复实现。"
            log_fn(message)
            return {"ticket_id": ticket_id, "status": "REVIEW", "message": message,
                    "review_error": str(error), "next_action": "处理复审错误后继续当前任务。"}
        if state["status"] == "REVISE":
            log_fn("复审发现可修复问题，正在交回原 Codex 会话。")


async def _run_plan(c, plan, options, agent_fn, phase_fn, log_fn, preflight, *, resolve=False):
    options = dict(options)
    while True:
        index, states = plans.position(c, plan)
        if plan.get("stopped") or index == len(states):
            result = plans.plan_report(c, plan)
            log_fn(result["message"])
            return result
        entry = plan["tasks"][index]
        if options.get("ticket") and options["ticket"] != entry["ticket_id"]:
            raise WorkflowError("Continue the plan's current Ticket; do not skip to another step.")
        try:
            state = plans.next_task(c, plan)
        except WorkflowError as exc:
            current = {"status": "BLOCKED", "message": str(exc),
                       "next_action": "核对当前文件与已保存的计划范围，再继续；后续任务尚未启动。"}
            result = plans.plan_report(c, plan, current)
            log_fn(result["message"])
            return result
        def step_phase(name):
            phase_fn(f"{entry['key']} · {index + 1}/{len(states)} · {name}")
        result = await _run_ticket(c, state["id"], options, agent_fn, step_phase, log_fn, preflight, resolve=resolve)
        resolve = False
        options.pop("ticket", None)
        if result["status"] != "PASS" or not result.get("verified"):
            if result["status"] == "PASS":
                result = dict(result, status="BLOCKED")
            return plans.plan_report(c, plan, result)
        plans.checkpoint(c, plan)
        if index + 1 < len(states):
            log_fn(f"{entry['key']} 已通过，继续下一项。")
        # The next iteration creates its baseline only after the preceding PASS.


async def run_workflow(*, agent_fn, phase_fn, log_fn, workflow_args, workspaces=(), controller=None, preflight=True):
    options = dict(workflow_args or {})
    if "codex" in options:
        options["codex"] = codex_settings(options["codex"])
    validate_mode(options.get("progress", "stages"))
    action = options.get("action", "continue")
    if action == "resume":
        action = "continue"
    if action not in {"start", "continue", "status", "resolve", "stop_plan"}:
        raise WorkflowError("Choose start, continue, status, resolve, or stop_plan.")
    if options.get("task") is not None and options.get("plan") is not None:
        raise WorkflowError("Start accepts either one task or one agreed plan, not both.")
    if (options.get("task") is not None or options.get("plan") is not None) and action != "start":
        raise WorkflowError("A new contract must use start; continue cannot silently replace the current task or plan.")
    repo = options.get("repo") or (workspaces[0] if workspaces else str(Path.cwd()))
    c = controller or Controller(repo, timeout=int(options.get("timeout", 900)))
    if action == "status":
        plan = plans.load_plan(c, options.get("plan_id")) if not options.get("ticket") else None
        ticket_id = active_id(c, options.get("ticket"))
        result = plans.plan_report(c, plan) if plan else report(c, c.load(ticket_id) if ticket_id else None)
        log_fn(result["message"])
        return result
    # This lock spans review awaits and PASS-to-next transitions. The original
    # worktree.lock remains separate so Controller methods can acquire it normally.
    with worktree_lock(c.root / "workflow.lock"):
        plan = plans.load_plan(c, options.get("plan_id"))
        active_plan = plans.load_plan(c)
        if plans.unfinished(c, active_plan) and plan and active_plan["id"] != plan["id"]:
            raise WorkflowError("Another unfinished plan owns this worktree. Continue its current step first.")
        if action != "start" and options.get("ticket") and plan and not plans.unfinished(c, plan):
            plan = None  # An explicit single Ticket must not be hidden by an old plan.
        if action == "start":
            if options.get("ticket") or options.get("plan_id"):
                raise WorkflowError("Start accepts a new contract; use continue with saved identifiers.")
            override = options.pop("codex", {})
            if options.get("plan") is not None:
                plan = plans.start_plan(c, options["plan"], codex=override)
            else:
                if plans.unfinished(c, plan):
                    raise WorkflowError("An unfinished plan already owns this worktree. Continue it or explicitly stop that plan first.")
                task = options.get("task")
                if override and isinstance(task, dict):
                    task = dict(task, codex=codex_settings(task.get("codex"), override))
                state = start_task(c, task)
                if plan:
                    atomic_json(c.root / "active-plan.json", {"plan_id": None})
                return await _run_ticket(c, state["id"], options, agent_fn, phase_fn, log_fn, preflight)
        if action == "stop_plan":
            if not plan:
                raise WorkflowError("There is no saved plan to stop.")
            plans.stop_plan(c, plan, options.get("reason"))
            return plans.plan_report(c, plan)
        if plan:
            if plans.unfinished(c, plan):
                if active_id(c) not in {entry["ticket_id"] for entry in plan["tasks"]}:
                    c._active()
                atomic_json(c.root / "active-plan.json", {"plan_id": plan["id"]})
            return await _run_plan(c, plan, options, agent_fn, phase_fn, log_fn, preflight, resolve=action == "resolve")
        ticket_id = active_id(c, options.get("ticket"))
        if ticket_id is None:
            result = report(c)
            log_fn(result["message"])
            return result
        return await _run_ticket(c, ticket_id, options, agent_fn, phase_fn, log_fn, preflight, resolve=action == "resolve")
