"""Offline v2 stdio protocol double; no external calls."""
import json
from pathlib import Path
import sys
import uuid


def main():
    if sys.argv[1:2] != ["app-server"]:
        from fake_codex import main as exec_main
        return exec_main()
    root = Path.cwd()/".git"
    control = root/"fake-app-control.json"
    mode = json.loads(control.read_text(encoding="utf-8")) if control.exists() else {}
    state_path = root/"fake-app-thread.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    ledger = root/"fake-app-calls.jsonl"
    pending = None
    def emit(value):
        print(json.dumps(value), flush=True)
    def record(request):
        with ledger.open("a", encoding="utf-8") as f:
            f.write(json.dumps(request) + "\n")
    def response(rid, result):
        emit({"id": rid, "result": result})
    def error(rid, code, message):
        emit({"id": rid, "error": {"code": code, "message": message}})
    def notify(method, **params):
        emit({"method": method, "params": params})
    def save():
        state_path.write_text(json.dumps(state), encoding="utf-8")
    def thread():
        return {"id": state.get("id"), "status": {"type": "idle"}, "turns": state.get("turns", [])}
    def finish(rid, params):
        tid = str(uuid.uuid4())
        report = {"outcome": "done", "summary": "Fixture implementation",
                  "changes": ["value.py"], "checks": [], "issue": None}
        (Path.cwd()/"value.py").write_text("VALUE = " + str(len(state["turns"])+1) + "\n", encoding="utf-8")
        turn = {"id": tid, "status": "completed", "error": None, "items": [
            {"type": "agentMessage", "phase": "final_answer", "text": json.dumps(report)}]}
        state["turns"].insert(0, turn)
        save()
        notify("turn/started", threadId=state["id"], turn={"id": tid, "status": "inProgress"})
        if mode.get("ambiguous"):
            error(rid, -32603, "ambiguous submission")
            return
        response(rid, {"turn": {"id": tid, "status": "inProgress"}})
        notify("item/completed", threadId=state["id"], turnId=tid, item={
            "type": "fileChange", "changes": [{"path": "value.py", "kind": "update"}]})
        notify("turn/completed", threadId=state["id"], turn=turn)
    for line in sys.stdin:
        msg = json.loads(line)
        record(msg)
        method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method is None:
            if pending:
                if msg.get("result") != {"decision": "decline"}:
                    return 64
                rid, params = pending
                pending = None
                finish(rid, params)
            continue
        if method == "initialize":
            if mode.get("init_error"):
                error(rid, -32601, "not supported")
            else:
                response(rid, {"codexHome": "fixture"})
        elif method == "initialized":
            continue
        elif method == "config/read":
            response(rid, {"config": {"model": "fixture-model", "model_provider": None,
                                      "model_reasoning_effort": "low", "secret": "NOT_FOR_LOGS"}})
        elif method == "modelProvider/list":
            response(rid, {"data": [{"id": "openai"}, {"id": "proxy"}]})
        elif method == "model/list":
            response(rid, {"data": [{"id": name, "model": name,
                                    "supportedReasoningEfforts": [{"reasoningEffort": "low"},
                                                                 {"reasoningEffort": "high"}]}
                                   for name in ("fixture-model", "fixture-upgrade")], "nextCursor": None})
        elif method == "account/rateLimits/read":
            response(rid, {"ordinaryUsageAllowed": True, "rateLimits": {
                "primary": {"usedPercent": 4, "windowDurationMins": 300},
                "secondary": {"usedPercent": 29, "windowDurationMins": 10080}, "planType": "plus"}})
        elif method in ("thread/start", "thread/resume"):
            if method == "thread/start":
                state = {"id": str(uuid.uuid4()), "turns": []}
                save()
            elif params.get("threadId") != state.get("id"):
                error(rid, -32600, "bad thread")
                continue
            response(rid, {"thread": thread(), "model": params.get("model", "fixture-model"),
                            "modelProvider": mode.get("provider", "openai"), "reasoningEffort": "low"})
        elif method == "thread/read":
            response(rid, {"thread": thread()})
        elif method == "thread/turns/list":
            response(rid, {"data": state.get("turns", [])})
        elif method == "turn/start":
            if params.get("threadId") != state.get("id") or not params.get("outputSchema"):
                return 64
            if mode.get("approval"):
                pending = (rid, params)
                emit({"id": "server-approval", "method": "item/commandExecution/requestApproval",
                      "params": {"threadId": state["id"]}})
            else:
                finish(rid, params)
        else:
            error(rid, -32601, "unsupported fixture request")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
