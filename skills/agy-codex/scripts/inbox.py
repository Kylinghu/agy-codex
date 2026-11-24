"""Ticket-local Maildir. Producers never take workflow/worktree locks.

Each Turn has a unique pending directory. Sealing renames the directory, so a
publish either belongs to that sealed batch or fails; it cannot silently land
in a later Ticket/Turn. Processing and acknowledgements are durable evidence.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from controller import WorkflowError, _metadata_io, atomic_json, now, read_json

MAX_MESSAGE_LENGTH = 8192
MAX_QUEUE_TURNS = 2
MAX_MESSAGES_PER_WINDOW = 32
ACTIONS = ("queue", "steer", "interrupt")


def root(c, ticket_id):
    return c.ticket_dir(ticket_id)/"inbox"


def open_window(c, state):
    wid = uuid.uuid4().hex
    directory = root(c, state["id"])/wid
    for name in ("pending", "processing", "ack"):
        (directory/name).mkdir(parents=True, exist_ok=False)
    state["inbox_window"] = wid
    c.save(state, "inbox_opened")
    return directory


def publish(c, ticket_id, action, text=""):
    if action not in ACTIONS:
        raise WorkflowError("Inbox action must be queue, steer, or interrupt.")
    if not isinstance(text, str) or len(text) > MAX_MESSAGE_LENGTH or (action != "interrupt" and not text.strip()):
        raise WorkflowError("Inbox message must be nonempty (except interrupt) and at most 8192 characters.")
    state = c.load(ticket_id)
    wid = state.get("inbox_window")
    if (state["status"] != "RUNNING" or state.get("backend") != "app-server"
            or not isinstance(wid, str) or not re.fullmatch(r"[0-9a-f]{32}", wid)):
        raise WorkflowError("No open App Server Turn inbox; current task was not changed.")
    directory = root(c, ticket_id)/wid
    pending = directory/"pending"
    if not pending.is_dir():
        raise WorkflowError("The Turn inbox has closed; no message was submitted.")
    mid = uuid.uuid4().hex
    message = {"id": mid, "ticket_id": ticket_id, "round": state["round"], "window": wid,
               "action": action, "text": text.strip(), "created_at": now(), "created_ns": time.time_ns()}
    # Temporary file is OUTSIDE pending. Never recreate a sealed pending directory.
    fd, temp = tempfile.mkstemp(prefix="publish-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(message, stream, ensure_ascii=False)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        try:
            _metadata_io(lambda: os.replace(temp, pending/(mid+".json")))
        except FileNotFoundError as exc:
            raise WorkflowError("The Turn inbox closed before publication; no message was submitted.") from exc
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return dict(message, status="queued")


def seal(c, state, directory):
    pending, sealed = directory/"pending", directory/"sealed"
    if pending.exists():
        _metadata_io(lambda: os.replace(pending, sealed))
    if state.get("inbox_window") == directory.name:
        state["inbox_window"] = None
        c.save(state, "inbox_sealed")
    return sealed


def records(directory, name):
    result = []
    for path in sorted((directory/name).glob("*.json")):
        try:
            message = read_json(path)
        except FileNotFoundError:  # Reader raced an atomic claim/seal.
            continue
        result.append((path, message))
    return sorted(result, key=lambda pair: (pair[1].get("created_ns", 0), pair[1].get("id", "")))


def claim(directory, path):
    destination = directory/"processing"/path.name
    try:
        _metadata_io(lambda: os.replace(path, destination))
    except FileNotFoundError:
        return None
    return read_json(destination)


def ack(c, message, status, **extra):
    directory = root(c, message["ticket_id"])/message["window"]
    value = dict(message, status=status, updated_at=now(), **extra)
    atomic_json(directory/"ack"/(message["id"]+".json"), value)
    return value


def requeue(directory, message):
    # Same ID and durable payload; action changes only after the precise stale
    # steer response. The original requested action remains visible.
    source = directory/"processing"/(message["id"]+".json")
    atomic_json(source, dict(message, delivery_action="queue", stale_steer=True))
    _metadata_io(lambda: os.replace(source, directory/"pending"/source.name))


def history(c, ticket_id):
    """Read-only, best-effort live view; ACK takes precedence over other locations."""
    directory = root(c, ticket_id)
    result = {}
    for window in sorted(directory.glob("*")):
        if not window.is_dir():
            continue
        for name, status in (("pending", "queued"), ("sealed", "queued"),
                             ("processing", "delivery_unknown"), ("ack", None)):
            for _, message in records(window, name):
                if message.get("ticket_id") != ticket_id or not message.get("id"):
                    continue
                result[message["id"]] = dict(message, status=status or message.get("status"))
    return sorted(result.values(), key=lambda m: (m.get("created_ns", 0), m["id"]))


def review_messages(c, ticket_id):
    # Accepted is a transport fact, not proof the instruction was implemented.
    return [m for m in history(c, ticket_id) if m["status"] in
            ("rpc_accepted", "turn_completed", "turn_interrupted", "turn_failed",
             "delivery_unknown", "deferred", "rejected")]


def drain(c, state, directory, *, allow_queue, reason):
    """Seal the active window and claim its final batch exactly once."""
    seal(c, state, directory)
    queued = []
    late_interrupt = False
    for path, _ in records(directory, "sealed"):
        message = claim(directory, path)
        if message is None:
            continue
        action = message.get("delivery_action", message["action"])
        if action == "interrupt":
            late_interrupt = True
            ack(c, message, "deferred", reason="Turn already ended; pause before further work.")
        elif allow_queue and len(queued) < MAX_MESSAGES_PER_WINDOW:
            queued.append(message)
        else:
            ack(c, message, "deferred", reason=reason)
    if late_interrupt:
        for message in queued:
            ack(c, message, "deferred", reason="User interrupted before the supplement Turn.")
        queued = []
    return queued, late_interrupt
