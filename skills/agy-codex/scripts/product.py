"""Small user-facing layer over the serial controller; no model or service SDK."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

from controller import WorkflowError, encode, git, read_json


SKILL_ROOT = Path(__file__).resolve().parent.parent


def contract(name):
    return (SKILL_ROOT / "references" / (name + ".md")).read_text(encoding="utf-8")


def active_id(controller, explicit=None):
    if explicit:
        controller.ticket_dir(explicit)  # Validate before using the identifier.
        return explicit
    path = controller.root / "active.json"
    return read_json(path)["ticket_id"] if path.exists() else None


def _plan_ref(controller, value):
    if not isinstance(value, dict) or not isinstance(value.get("path"), str):
        raise WorkflowError("plan_ref requires a repository-relative path.")
    name = value["path"].replace("\\", "/")
    relative = PurePosixPath(name)
    if (relative.is_absolute() or not relative.parts or ".." in relative.parts
            or ":" in name or relative.parts[0] == ".git"):
        raise WorkflowError("The plan must be a file inside this worktree.")
    path = (controller.repo / name).resolve()
    try:
        path.relative_to(controller.repo)
    except ValueError as exc:
        raise WorkflowError("The plan reference resolves outside this worktree.") from exc
    if not path.is_file():
        raise WorkflowError("The referenced plan does not exist: " + name)
    section = value.get("section", "")
    if not isinstance(section, str):
        raise WorkflowError("plan_ref.section must be a task ID or heading string.")
    return {"path": relative.as_posix(), "section": section,
            "sha256_at_start": hashlib.sha256(path.read_bytes()).hexdigest()}


def start_task(controller, task, *, expected_sha256=None):
    """The Router supplies this object internally; users do not author JSON."""
    if not isinstance(task, dict):
        raise WorkflowError("Start needs the scoped task prepared from the user's goal or plan.")
    spec = json.loads(json.dumps(task))
    supplied_id = spec.pop("id", None)
    spec.pop("request_fingerprint", None)
    if not spec.get("allowed_paths"):
        raise WorkflowError("Choose the files or directories this task may change before starting.")
    if "plan_ref" in spec:
        # The request key uses the pointer, not generated revision metadata. This
        # keeps retries idempotent when the Worker updates PLAN.md within scope.
        ref = spec["plan_ref"]
        if not isinstance(ref, dict):
            raise WorkflowError("plan_ref must be an object with path and optional section.")
        spec["plan_ref"] = {"path": ref.get("path"), "section": ref.get("section", "")}
    spec.setdefault("max_rounds", 3)
    head = git(controller.repo, "rev-parse", "HEAD").decode().strip()
    key = hashlib.sha256((head + "\n" + encode(spec)).encode("utf-8")).hexdigest()
    ticket_id = supplied_id or "TASK-" + key[:12]
    existing = controller.ticket_dir(ticket_id) / "ticket.json"
    if existing.exists():
        state = controller.load(ticket_id)
        if state["spec"].get("request_fingerprint") != key:
            raise WorkflowError("This task identifier already belongs to a different contract. Continue it or use a new scoped task.")
        return state
    dependencies = spec.get("depends_on", [])
    if not isinstance(dependencies, list) or not all(isinstance(x, str) for x in dependencies):
        raise WorkflowError("depends_on must be a list of existing task IDs.")
    for dependency in dependencies:
        try:
            previous = controller.load(dependency)
        except FileNotFoundError as exc:
            raise WorkflowError("The dependency has no recorded result: " + dependency) from exc
        if previous["status"] != "PASS":
            raise WorkflowError("The dependency is not complete: " + dependency)
    if "plan_ref" in spec:
        spec["plan_ref"] = _plan_ref(controller, spec["plan_ref"])
    spec.update(id=ticket_id, request_fingerprint=key)
    return controller.create(spec, expected_sha256=expected_sha256)


def usage(controller, state):
    totals = {}
    reported = 0
    prompt_chars = []
    for number in range(1, state["round"] + 1):
        directory = controller.ticket_dir(state["id"]) / "rounds" / f"{number:02d}"
        summary = directory / "summary.json"
        if summary.exists():
            counters = read_json(summary).get("usage", {})
            if isinstance(counters, dict) and counters:
                reported += 1
                for key, value in counters.items():
                    if type(value) in (int, float) and value >= 0:
                        totals[key] = totals.get(key, 0) + value
        prompt = directory / "prompt.txt"
        if prompt.exists():
            prompt_chars.append({"round": number, "characters": len(prompt.read_text(encoding="utf-8"))})
    return {"codex": totals or None, "rounds_with_usage": reported,
            "rounds_started": state["round"], "worker_prompt_characters": prompt_chars,
            "agy": None, "note": "AGY usage is unavailable here. Characters are not tokens; cached input is a subset, not an extra token count."}


def report(controller, state=None):
    if state is None:
        return {"status": "IDLE", "message": "当前项目还没有 AGY Codex 任务。可以给出目标，或指定已有计划中的任务。"}
    labels = {"READY": "准备执行", "RUNNING": "正在实现", "REVIEW": "等待复审",
              "REVISE": "准备修复", "PASS": "已完成", "ESCALATE": "需要处理"}
    directory = controller._round_dir(state)
    packet_path = directory / "review-packet.json"
    packet = read_json(packet_path) if packet_path.exists() else {}
    tests = packet.get("tests", [])
    current = None if state["status"] == "RUNNING" else controller.snapshot_for(state)["sha256"] == state["expected_sha256"]
    progress_path = controller.ticket_dir(state["id"]) / "progress.json"
    progress = read_json(progress_path) if progress_path.exists() else None
    title = state["spec"]["title"]
    text = f"{title}：{labels.get(state['status'], state['status'])}，已实施 {state['round']}/{state['max_rounds']} 轮。"
    if tests:
        text += f"验收命令 {sum(x.get('exit_code') == 0 for x in tests)}/{len(tests)} 通过。"
        skipped = sum(bool(x.get("skipped")) for x in tests)
        if skipped:
            text += f"其中 {skipped} 项因已记录的权限问题未运行。"
    recovery = bool(state.get("recoverable_interruption"))
    actions = {
        "READY": "继续执行当前任务。", "RUNNING": "继续时先检查原进程，再恢复原会话。",
        "REVIEW": "继续只会复审当前结果，不会重复实现。", "REVISE": "继续将在原会话修复具体问题。",
        "PASS": "可以查看结果或开始下一项任务；现有修改会保留为新任务的起点。",
        "ESCALATE": "按具体阻塞原因处理，保留现有成果与证据。",
    }
    next_action = "可以说“继续”，工具将检查进程并恢复原会话。" if recovery else actions[state["status"]]
    if not current and state["status"] != "RUNNING":
        text += "保存的结果对应先前快照；此后工作树已有变化，旧验收不能代表当前文件。"
        if state["status"] == "REVIEW":
            next_action = "先核对外部改动；确认属于当前授权范围后刷新验收证据，再继续复审。"
        elif state["status"] in {"READY", "REVISE"}:
            next_action = "先核对工作树变化与任务范围，不直接继续执行或覆盖现有改动。"
    blocker = (state.get("last_review") or {}).get("blocker") if state["status"] == "ESCALATE" and state.get("stop_origin") != "controller" else None
    if state.get("resolution_allowed") and current:
        next_action = blocker.get("question") or blocker.get("detail") or next_action
    return {"ticket_id": state["id"], "title": title, "status": state["status"],
            "round": state["round"], "max_rounds": state["max_rounds"],
            "message": text, "summary": state.get("reason") if state["status"] == "ESCALATE" else
                    (state.get("last_review") or {}).get("summary") or state.get("reason"),
            "reason": state.get("reason"), "review_summary": (state.get("last_review") or {}).get("summary"),
            "next_action": next_action, "snapshot_current": current,
            "verified": state["status"] == "PASS" and current,
            "blocker": blocker, "resolution_allowed": bool(state.get("resolution_allowed")),
            "worker_report": packet.get("worker_report"), "tests": tests,
            "changed_files": packet.get("changed_files", []),
            "recoverable": recovery, "usage": usage(controller, state), "progress": progress,
            "evidence_directory": str(controller.ticket_dir(state["id"]))}


def review_input(controller, state, packet, fresh):
    """Keep the complete packet on disk; a live reviewer gets only new input."""
    if fresh:
        data = dict(packet, review_mode="full")
    else:
        keys = ("ticket_id", "round", "snapshot_sha256", "worktree", "changed_files",
                "tests", "all_tests_passed", "diff_path", "summary_path", "last_message_path",
                "diff_basis", "diff_metadata_path", "worker_report")
        data = {key: packet.get(key) for key in keys}
        data.update(review_mode="followup", contract_path=str(controller._contract_path(state)),
                    unresolved_findings=(state.get("last_review") or {}).get("findings", []))
        previous_path = controller.ticket_dir(state["id"])/"rounds"/f"{state['round']-1:02d}"/"snapshot.json"
        if previous_path.exists():
            previous = read_json(previous_path)
            current = read_json(controller._round_dir(state)/"snapshot.json")
            data["files_changed_since_review"] = controller._changed(current, previous)
    # Decisions outlive last_review, which later REVISE verdicts replace. Rebuild
    # from the Ticket for both modes, including packets saved before this field.
    data["resolutions"] = state.get("resolutions", [])
    return data


def reviewer_prompt(data):
    mode = data["review_mode"]
    opening = contract("review") if mode == "full" else (
        "Continue the same read-only Spec and Standards review. Start with files_changed_since_review "
        "and unresolved_findings. Reuse the unchanged contract already in context; read contract_path "
        "if context is missing. Retain recorded resolutions within that contract; they do not change permissions. "
        "The diff_path is the COMPLETE cumulative worktree diff, not a delta patch. "
        "Check interactions with previously reviewed changes before PASS. Do not edit, run tests, "
        "or start another agent. Copy the exact ticket_id, round and snapshot_sha256. Return the schema "
        "with both axes and stable finding IDs."
        " Inspect worker_report: handle routine in-scope engineering choices yourself via an actionable REVISE. "
        "Keep genuine product decisions and permission blockers explicit as ESCALATE with blocker; no PASS for unfinished work."
    )
    return opening.rstrip() + "\n\nREVIEW_PACKET\n" + json.dumps(data, ensure_ascii=False, indent=2)
