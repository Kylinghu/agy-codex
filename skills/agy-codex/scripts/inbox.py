"""Run/Ticket-local Maildir. Producers never take workflow/workspace locks.

Each Turn has a unique pending directory. Sealing renames the directory, so a
publish either belongs to that sealed batch or fails; it cannot silently land
in a later Run/Turn. Processing and acknowledgements are durable evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from runtime import WorkflowError, _metadata_io, atomic_json, now, read_json

MAX_MESSAGE_LENGTH = 8192
MAX_QUEUE_TURNS = 2
MAX_MESSAGES_PER_WINDOW = 32
ACTIONS = ("queue", "steer", "interrupt")


@dataclass
class InboxTarget:
    inbox_dir: Path
    owner_id: str
    load_state: object
    save_state: object
    round_number: int = 1


def _coerce_target(target, owner_id=None):
    if isinstance(target, InboxTarget):
        return target
    # Backward compatibility with legacy (Controller, ticket_id) callers and tests.
    if owner_id is None:
        raise WorkflowError("Inbox target requires an owner ID.")
    return InboxTarget(
        inbox_dir=target.ticket_dir(owner_id) / "inbox",
        owner_id=owner_id,
        load_state=lambda: target.load(owner_id),
        save_state=lambda state, event=None: target.save(state, event),
    )


def root(target, owner_id=None):
    return _coerce_target(target, owner_id).inbox_dir


def open_window(target, state):
    t = target if isinstance(target, InboxTarget) else _coerce_target(target, state["id"])
    wid = uuid.uuid4().hex
    directory = t.inbox_dir / wid
    for name in ("pending", "processing", "ack"):
        (directory / name).mkdir(parents=True, exist_ok=False)
    state["inbox_window"] = wid
    t.save_state(state, "inbox_opened")
    return directory


def _is_active_status(status):
    return str(status or "").lower() == "running"


def publish(target, owner_id_or_action, action_or_text="", text=""):
    if isinstance(target, InboxTarget):
        t = target
        action, msg_text = owner_id_or_action, action_or_text
    else:
        t = _coerce_target(target, owner_id_or_action)
        action, msg_text = action_or_text, text

    if action not in ACTIONS:
        raise WorkflowError("Inbox action must be queue, steer, or interrupt.")
    if not isinstance(msg_text, str) or len(msg_text) > MAX_MESSAGE_LENGTH or (action != "interrupt" and not msg_text.strip()):
        raise WorkflowError("Inbox message must be nonempty (except interrupt) and at most 8192 characters.")
    state = t.load_state()
    wid = state.get("inbox_window")
    if (not _is_active_status(state.get("status")) or state.get("backend") != "app-server"
            or not isinstance(wid, str) or not re.fullmatch(r"[0-9a-f]{32}", wid)):
        raise WorkflowError("No open App Server Turn inbox; current task was not changed.")
    directory = t.inbox_dir / wid
    pending = directory / "pending"
    if not pending.is_dir():
        raise WorkflowError("The Turn inbox has closed; no message was submitted.")
    mid = uuid.uuid4().hex
    message = {
        "id": mid,
        "owner_id": t.owner_id,
        "ticket_id": t.owner_id,  # Retained for backward-compatible readers.
        "round": state.get("round", t.round_number),
        "window": wid,
        "action": action,
        "text": msg_text.strip(),
        "created_at": now(),
        "created_ns": time.time_ns(),
    }
    # Temporary file is OUTSIDE pending. Never recreate a sealed pending directory.
    fd, temp = tempfile.mkstemp(prefix="publish-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(message, stream, ensure_ascii=False)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        try:
            _metadata_io(lambda: os.replace(temp, pending / (mid + ".json")))
        except FileNotFoundError as exc:
            raise WorkflowError("The Turn inbox closed before publication; no message was submitted.") from exc
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return dict(message, status="queued")


def seal(target, state, directory):
    t = target if isinstance(target, InboxTarget) else _coerce_target(target, state["id"])
    pending, sealed = directory / "pending", directory / "sealed"
    if pending.exists():
        _metadata_io(lambda: os.replace(pending, sealed))
    if state.get("inbox_window") == directory.name:
        state["inbox_window"] = None
        t.save_state(state, "inbox_sealed")
    return sealed


def records(directory, name):
    result = []
    for path in sorted((directory / name).glob("*.json")):
        try:
            message = read_json(path)
        except FileNotFoundError:  # Reader raced an atomic claim/seal.
            continue
        result.append((path, message))
    return sorted(result, key=lambda pair: (pair[1].get("created_ns", 0), pair[1].get("id", "")))


def claim(directory, path):
    destination = directory / "processing" / path.name
    try:
        _metadata_io(lambda: os.replace(path, destination))
    except FileNotFoundError:
        return None
    return read_json(destination)


def ack(target, message, status, **extra):
    owner = message.get("owner_id") or message.get("ticket_id")
    t = target if isinstance(target, InboxTarget) else _coerce_target(target, owner)
    directory = t.inbox_dir / message["window"]
    value = dict(message, status=status, updated_at=now(), **extra)
    atomic_json(directory / "ack" / (message["id"] + ".json"), value)
    return value


def requeue(directory, message):
    # Same ID and durable payload; action changes only after the precise stale
    # steer response. The original requested action remains visible.
    source = directory / "processing" / (message["id"] + ".json")
    atomic_json(source, dict(message, delivery_action="queue", stale_steer=True))
    _metadata_io(lambda: os.replace(source, directory / "pending" / source.name))


def history(target, owner_id=None):
    """Read-only, best-effort live view; ACK takes precedence over other locations."""
    t = target if isinstance(target, InboxTarget) else _coerce_target(target, owner_id)
    directory = t.inbox_dir
    result = {}
    if not directory.is_dir():
        return []
    for window in sorted(directory.glob("*")):
        if not window.is_dir():
            continue
        for name, status in (("pending", "queued"), ("sealed", "queued"),
                             ("processing", "delivery_unknown"), ("ack", None)):
            for _, message in records(window, name):
                msg_owner = message.get("owner_id") or message.get("ticket_id")
                if msg_owner != t.owner_id or not message.get("id"):
                    continue
                result[message["id"]] = dict(message, status=status or message.get("status"))
    return sorted(result.values(), key=lambda m: (m.get("created_ns", 0), m["id"]))


def review_messages(target, owner_id=None):
    # Accepted is a transport fact, not proof the instruction was implemented.
    return [m for m in history(target, owner_id) if m["status"] in
            ("rpc_accepted", "turn_completed", "turn_interrupted", "turn_failed",
             "queued", "delivery_unknown", "deferred", "rejected")]


def finish_accepted(target, owner_or_turn_id, turn_or_status, status=None):
    """Only native terminal evidence may advance an already accepted ACK."""
    if isinstance(target, InboxTarget):
        t = target
        turn_id, term_status = owner_or_turn_id, turn_or_status
    else:
        t = _coerce_target(target, owner_or_turn_id)
        turn_id, term_status = turn_or_status, status
    terminal = {"completed": "turn_completed", "interrupted": "turn_interrupted",
                "failed": "turn_failed"}.get(term_status)
    if not terminal:
        return
    for message in history(t):
        if message.get("status") == "rpc_accepted" and message.get("turn_id") == turn_id:
            ack(t, message, terminal, turn_id=turn_id)


def recover_window(target, state):
    """A crashed window is never replayed; retain uncertain claims and defer unclaimed input."""
    t = target if isinstance(target, InboxTarget) else _coerce_target(target, state["id"])
    wid = state.get("inbox_window")
    if not isinstance(wid, str) or not re.fullmatch(r"[0-9a-f]{32}", wid):
        return
    directory = t.inbox_dir / wid
    drain(t, state, directory, allow_queue=False,
          reason="Interrupted window closed during recovery; input was not submitted.")


def drain(target, state, directory, *, allow_queue, reason):
    """Seal the active window and claim its final batch exactly once."""
    t = target if isinstance(target, InboxTarget) else _coerce_target(target, state["id"])
    seal(t, state, directory)
    queued = []
    late_interrupt = False
    for path, _ in records(directory, "sealed"):
        message = claim(directory, path)
        if message is None:
            continue
        action = message.get("delivery_action", message["action"])
        if action == "interrupt":
            late_interrupt = True
            ack(t, message, "deferred", reason="Turn already ended; pause before further work.")
        elif allow_queue and len(queued) < MAX_MESSAGES_PER_WINDOW:
            queued.append(message)
        else:
            ack(t, message, "deferred", reason=reason)
    if late_interrupt:
        for message in queued:
            ack(t, message, "deferred", reason="User interrupted before the supplement Turn.")
        queued = []
    return queued, late_interrupt
