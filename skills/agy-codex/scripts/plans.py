"""Finite, ordered task contracts over the existing Ticket engine; no scheduler."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from controller import WorkflowError, atomic_json, encode, git, now, read_json, validate_spec, worktree_lock
from product import _plan_ref, active_id, report, start_task, usage


def plan_path(controller, plan_id):
    controller.ticket_dir(plan_id)  # Same contained identifier grammar.
    return controller.root / "plans" / plan_id / "plan.json"


def _unpublished_plan(controller):
    # Includes the case where the pointer still names an older completed plan.
    candidates = []
    for path in (controller.root / "plans").glob("*/plan.json"):
        candidate = load_plan(controller, path.parent.name)
        if unfinished(controller, candidate):
            candidates.append(candidate)
    if len(candidates) > 1:
        raise WorkflowError("Several unfinished plans exist; select the intended saved plan explicitly.")
    return candidates[0] if candidates else None


def load_plan(controller, plan_id=None):
    explicit = plan_id is not None
    if plan_id is None:
        pointer = controller.root / "active-plan.json"
        plan_id = read_json(pointer).get("plan_id") if pointer.exists() else None
    if not plan_id:
        # Recover the small publication window between manifest and pointer.
        return _unpublished_plan(controller)
    plan = read_json(plan_path(controller, plan_id))
    if (plan["id"] != plan_id or Path(plan["worktree"]).resolve() != controller.repo
            or Path(plan["git_dir"]).resolve() != controller.gitdir):
        raise WorkflowError("Plan belongs to a different worktree.")
    if not explicit and not unfinished(controller, plan):
        pending = _unpublished_plan(controller)
        if pending:
            return pending
        if active_id(controller) not in {entry["ticket_id"] for entry in plan["tasks"]}:
            return None
    return plan


def save_plan(controller, plan):
    plan["updated_at"] = now()
    atomic_json(plan_path(controller, plan["id"]), plan)


def request_contract(task):
    value = json.loads(json.dumps(task))
    value.pop("id", None)
    value.pop("request_fingerprint", None)
    value.setdefault("max_rounds", 3)
    value.setdefault("depends_on", [])
    if "plan_ref" in value:
        ref = value["plan_ref"]
        if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
            raise WorkflowError("plan_ref requires a repository-relative path.")
        value["plan_ref"] = {"path": ref["path"], "section": ref.get("section", "")}
    return value


def position(controller, plan):
    """Ticket records, never the cached cursor, decide the completed prefix."""
    states = []
    for entry in plan["tasks"]:
        path = controller.ticket_dir(entry["ticket_id"]) / "ticket.json"
        state = controller.load(entry["ticket_id"]) if path.exists() else None
        if state and request_contract(state["spec"]) != entry["task"]:
            raise WorkflowError("Saved Ticket no longer matches its frozen plan contract.")
        states.append(state)
    index = 0
    while index < len(states) and states[index] and states[index]["status"] == "PASS":
        index += 1
    if any(state is not None for state in states[index + 1:]):
        raise WorkflowError("A later plan Ticket exists before its predecessor passed.")
    return index, states


def unfinished(controller, plan):
    return bool(plan and not plan.get("stopped") and position(controller, plan)[0] < len(plan["tasks"]))


def start_plan(controller, value):
    if not isinstance(value, dict) or not isinstance(value.get("title"), str) or not value["title"].strip():
        raise WorkflowError("A plan needs a title and the already-agreed ordered task contracts.")
    rows = value.get("tasks")
    if not isinstance(rows, list) or not rows:
        raise WorkflowError("A plan needs a nonempty ordered tasks list.")
    source = _plan_ref(controller, value["plan_ref"]) if value.get("plan_ref") else None
    keys, tasks = [], []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("key"), str) or not row["key"].strip():
            raise WorkflowError("Each plan step needs a nonempty key and task.")
        key = row["key"]
        if key in keys:
            raise WorkflowError("Plan step keys must be unique.")
        if not isinstance(row.get("task"), dict) or row["task"].get("id"):
            raise WorkflowError("Plan steps contain task contracts; Ticket IDs are assigned automatically.")
        task = request_contract(row["task"])
        if not task.get("allowed_paths"):
            raise WorkflowError("Every plan task needs allowed_paths.")
        validate_spec(task)
        if "plan_ref" not in task and source:
            task["plan_ref"] = {"path": source["path"], "section": key}
        if "plan_ref" in task:
            ref = _plan_ref(controller, task["plan_ref"])
            task["plan_ref"] = {"path": ref["path"], "section": ref["section"]}
        dependencies = task["depends_on"]
        if not isinstance(dependencies, list) or not all(isinstance(dep, str) and dep for dep in dependencies):
            raise WorkflowError("Plan dependencies must be step keys or existing Ticket IDs.")
        keys.append(key)
        tasks.append(task)
    head = git(controller.repo, "rev-parse", "HEAD").decode().strip()
    request = {"head": head, "title": value["title"], "keys": keys, "tasks": tasks,
               "plan_ref": {k: source[k] for k in ("path", "section")} if source else None}
    fingerprint = hashlib.sha256(encode(request).encode("utf-8")).hexdigest()
    plan_id = "PLAN-" + fingerprint[:12]
    mapping = {key: f"{plan_id}-{index + 1:03d}" for index, key in enumerate(keys)}
    entries = []
    for index, (key, task) in enumerate(zip(keys, tasks)):
        resolved = []
        for dependency in task["depends_on"]:
            if dependency in mapping:
                if keys.index(dependency) >= index:
                    raise WorkflowError("Plan dependencies must refer only to earlier steps, never self or future steps.")
                resolved.append(mapping[dependency])
            else:
                try:
                    previous = controller.load(dependency)
                except FileNotFoundError as exc:
                    raise WorkflowError("External plan dependency has no recorded result: " + dependency) from exc
                if previous["status"] != "PASS":
                    raise WorkflowError("External plan dependency is not complete: " + dependency)
                resolved.append(dependency)
        task["depends_on"] = resolved
        entries.append({"key": key, "ticket_id": mapping[key], "task": task})
    with worktree_lock(controller.lock):
        active = load_plan(controller)
        if unfinished(controller, active) and active["id"] != plan_id:
            raise WorkflowError("An unfinished plan already owns this worktree. Continue it or explicitly stop that plan first.")
        path = plan_path(controller, plan_id)
        if path.exists():
            plan = load_plan(controller, plan_id)
            if plan["request_fingerprint"] != fingerprint:
                raise WorkflowError("Plan identity conflicts with its saved contract.")
            if active_id(controller) not in {entry["ticket_id"] for entry in plan["tasks"]}:
                controller._active()
        else:
            controller._active()
            snapshot = controller.snapshot()
            plan = {"schema_version": 1, "id": plan_id, "title": value["title"],
                    "request_fingerprint": fingerprint, "worktree": str(controller.repo),
                    "git_dir": str(controller.gitdir), "plan_ref": source,
                    "initial_sha256": snapshot["sha256"], "tasks": entries,
                    "cursor": 0, "stopped": False, "created_at": now()}
            save_plan(controller, plan)
        atomic_json(controller.root / "active-plan.json", {"plan_id": plan_id})
        return plan


def next_task(controller, plan):
    index, states = position(controller, plan)
    if plan.get("stopped") or index == len(states):
        return None
    if states[index] is not None:
        return states[index]
    expected = states[index - 1]["expected_sha256"] if index else plan["initial_sha256"]
    entry = plan["tasks"][index]
    return start_task(controller, dict(entry["task"], id=entry["ticket_id"]), expected_sha256=expected)


def checkpoint(controller, plan):
    plan["cursor"] = position(controller, plan)[0]
    save_plan(controller, plan)


def stop_plan(controller, plan, reason):
    if not isinstance(reason, str) or not reason.strip():
        raise WorkflowError("Stopping a plan needs the user's stated reason.")
    index, states = position(controller, plan)
    if index < len(states) and states[index] is not None:
        controller.escalate(states[index]["id"], reason)
    plan.update(stopped=True, stop_reason=reason)
    save_plan(controller, plan)


def plan_report(controller, plan, current_result=None):
    index, states = position(controller, plan)
    complete = index == len(states)
    current_state = states[-1] if complete else states[index]
    current = current_result if current_result is not None else report(controller, current_state) if current_state else None
    status = "STOPPED" if plan.get("stopped") else (
        ("PASS" if current.get("verified") else "BLOCKED") if complete else (current or {}).get("status", "READY"))
    message = f"{plan['title']}：已完成 {index}/{len(states)} 项。"
    if status == "STOPPED":
        message += "计划已停止，已有成果和恢复记录保留。"
    elif complete:
        message += "计划全部通过。" if current.get("verified") else "各项已有通过记录；当前文件已变化，需核对最新结果。"
    else:
        entry = plan["tasks"][index]
        message += f"当前 {entry['key']}：{entry['task']['title']}。"
        if current:
            message += current["message"]
    totals = {}
    for state in states:
        if state:
            for key, value in (usage(controller, state)["codex"] or {}).items():
                totals[key] = totals.get(key, 0) + value
    return {"kind": "plan", "plan_id": plan["id"], "title": plan["title"], "status": status,
            "completed": index, "total": len(states), "message": message,
            "verified": bool(complete and status == "PASS" and current.get("verified")),
            "current_key": None if complete else plan["tasks"][index]["key"], "current": current,
            "next_action": ("核对计划结束后的文件变化；已有通过记录不能验证当前文件。" if complete and status == "BLOCKED"
                            else (current or {}).get("next_action")), "progress": (current or {}).get("progress"),
            "usage": {"codex": totals or None, "agy": None},
            "tasks": [{"key": entry["key"], "ticket_id": entry["ticket_id"], "title": entry["task"]["title"],
                       "status": state["status"] if state else "PENDING", "round": state["round"] if state else 0}
                      for entry, state in zip(plan["tasks"], states)]}
