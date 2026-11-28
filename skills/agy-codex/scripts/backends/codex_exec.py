"""Noninteractive Codex exec backend adapter (Git & non-Git)."""
from runtime import atomic_json, codex_exec_flags, now, run_process


def run(ws, state, save_fn, launcher, requested, artifact_dir, prompt, on_line, *,
        sandbox="workspace-write", output_schema_path=None, timeout=900, cancel=None):
    argv = [*launcher, "-a", "never", "-s", sandbox, "exec"]
    thread_id = state.get("thread_id")
    if thread_id:
        argv.append("resume")
    argv += codex_exec_flags(requested)
    if not ws.is_git:
        argv.append("--skip-git-repo-check")
    argv += ["--json", "-o", str(artifact_dir / "last-message.md")]
    if output_schema_path is not None:
        argv += ["--output-schema", str(output_schema_path)]
    if thread_id:
        argv.append(thread_id)
    argv.append("-")
    atomic_json(artifact_dir / "invocation.json", {
        "backend": "exec", "argv": argv, "cwd": str(ws.root_path),
        "sandbox": sandbox, "started_at": now(), "codex_requested": requested,
    })
    state["backend"] = "exec"
    save_fn()

    def on_pid(pid, started):
        if started:
            state["child_pid"] = pid
            save_fn("process_started")
        else:
            state["last_child_pid"] = pid
            state["child_pid"] = None
            save_fn()

    return run_process(
        ws.root_path, argv, prompt,
        artifact_dir / "codex.jsonl", artifact_dir / "stderr.log",
        on_line=on_line, on_pid=on_pid, timeout=timeout, cancel=cancel,
    )
