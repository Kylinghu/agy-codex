"""Existing noninteractive execution adapter."""
from controller import atomic_json, codex_exec_flags, now


def run(c, state, launcher, requested, directory, prompt, on_line):
    argv = [*launcher, "-a", "never", "-s", "workspace-write", "exec"]
    if state["session_id"]:
        argv.append("resume")
    argv += codex_exec_flags(requested)
    argv += ["--json", "-o", str(directory/"last-message.md"),
             "--output-schema", str(directory/"worker-schema.json")]
    if state["session_id"]:
        argv.append(state["session_id"])
    argv.append("-")
    atomic_json(directory/"invocation.json", {"backend": "exec", "argv": argv,
                "cwd": str(c.repo), "started_at": now(), "codex_requested": requested})
    state["backend"] = "exec"
    c.save(state)
    return c._process(state, argv, prompt, directory/"codex.jsonl",
                      directory/"stderr.log", on_line)
