---
name: agy-codex
description: Run and coordinate Codex CLI from Antigravity with adaptive lightweight execution by default and optional Strict Workflow when formal Ticket/Plan/review auditing is requested.
---

# AGY Codex Execution Manual

Gemini decides what to do, communicates with the user, chooses model tiers, and checks results. Codex CLI executes implementation and edits project files. The shared Runtime guarantees reliable execution (Git and non-Git).

Resolve `SKILL_DIR` from this `SKILL.md` location and use a verified Python 3.10+ executable (`PYTHON`) directly. Do not assume `${PLUGIN_ROOT}` expands or that Windows `py`/`python` aliases work without checking.

## 1. Choose the Shortest Path

| User intent | Default action |
| --- | --- |
| Question, analysis, design discussion | Gemini answers directly. Do not launch Codex. |
| Single fix, feature, small refactor, or document task (Git or non-Git) | Run `user_cli.py run` once via `run_command`. No `/run-workflow`, no Ticket, no forced Reviewer. |
| Related follow-up task (same topic or shared source material) | Run `user_cli.py run --resume <RUN_ID\|last>` on the same native Thread with incremental instructions only. |
| Multi-file document suite (e.g. large specs/Excel/Word) | Parse source material once into structured intermediate facts (project cache or temp dir, not polluting deliverables), then continue in the same Thread. |
| Mid-flight clarification, correction, or pause | Run an independent `run_command` calling `user_cli.py inbox queue\|steer\|interrupt` (never kills the background task or takes locks). |
| Progress or saved runs | Run `user_cli.py status` (offline, zero model calls, zero full-repo SHA-256). |
| On-demand code/document check | Inspect changed files with `read_file` and run necessary checks; for an isolated Codex second pass, use a **fresh** `user_cli.py run --read-only` (never treat a writer's own session as independent review). |
| Explicit formal audit / agreed strict multi-step plan / continuing an active Strict Ticket | Read `references/host.md` and `references/planning.md`, then invoke `/run-workflow` with `scripts/agy_workflow.py`. |

Before starting a lightweight write run in a Git repo, if an active/recoverable Strict Ticket or unfinished Plan owns the worktree, continue or stop it rather than bypassing it.

## 2. Lightweight CLI (`scripts/user_cli.py`)

Five commands cover all normal work (both `--cwd` and `--repo` are accepted):

```powershell
# New task (Git or non-Git)
& $PYTHON "$SKILL_DIR/scripts/user_cli.py" run --cwd "$WORKSPACE" --prompt-file task.md --json

# Incremental continuation on the same native Thread (RUN_ID, THREAD_ID, or workspace 'last')
& $PYTHON "$SKILL_DIR/scripts/user_cli.py" run --cwd "$WORKSPACE" --resume RUN_ID --prompt "Continue with step 2" --json

# Read-only analysis or isolated review (fresh Thread, real sandbox=read-only)
& $PYTHON "$SKILL_DIR/scripts/user_cli.py" run --cwd "$WORKSPACE" --read-only --prompt-file review.md --json

# Live mid-flight control while run is active in background (App Server backend)
& $PYTHON "$SKILL_DIR/scripts/user_cli.py" inbox --cwd "$WORKSPACE" steer "Avoid Office COM; use openpyxl"
& $PYTHON "$SKILL_DIR/scripts/user_cli.py" inbox --cwd "$WORKSPACE" queue "Also check edge case X"
& $PYTHON "$SKILL_DIR/scripts/user_cli.py" inbox --cwd "$WORKSPACE" interrupt

# Offline status / reconcile ambiguous turn / model discovery / environment check
& $PYTHON "$SKILL_DIR/scripts/user_cli.py" status --cwd "$WORKSPACE" --json
& $PYTHON "$SKILL_DIR/scripts/user_cli.py" status --cwd "$WORKSPACE" --reconcile --json
& $PYTHON "$SKILL_DIR/scripts/user_cli.py" models --cwd "$WORKSPACE" --json
& $PYTHON "$SKILL_DIR/scripts/user_cli.py" doctor --cwd "$WORKSPACE" --json
```

### Host Async Execution

- Always run `user_cli.py run` as a **foreground** command through Antigravity's `run_command` (e.g., `WaitMsBeforeAsync=10000`, `NotificationTimeoutSeconds=120`).
- Fast runs finish in the same turn with zero extra turns. Longer runs automatically become host background tasks; wait for completion wakeup instead of polling in a loop.
- Use `manage_task(status)` or `user_cli.py status` only when checking live progress on demand; use `manage_task(kill)` for hard cancellation (Windows Job Object terminates the full process tree and releases locks).
- If a run exits with `status: "unknown"` (`turn_pending: true`), run `user_cli.py status --reconcile` before starting another run; never blindly replay an ambiguous turn.

## 3. Model Selection & Domain Tools

- Choose a tier sufficient for the task (`--tier economy|standard|heavy` or explicit `--model` / `--effort`). Do **not** default to `heavy` for routine edits or mechanical formatting; upgrade only when real complexity requires it.
- Call `user_cli.py models --json` only when selecting a tier or checking quota—not on every `resume` or `status`. Honor user-specified models/efforts and configured `allowed_providers`; never modify Codex's global config.
- Reuse project `AGENTS.md`, installed domain Skills (HWB, Office, etc.), and MCP servers. Do not build global startup gates for optional domain libraries.
- **Office / Windows Sandbox**: When a task specifically requires Office libraries (`python-docx`, `openpyxl`, etc.), probe once upfront instead of letting Codex hand-write fragile OOXML. Windows `workspace-write` sandbox blocks Office COM (`0x80080005`); do not retry COM loops or auto-escalate permissions. Distinguish **artifact defect**, **verification failure**, and **verification environment limitation**—preserve valid generated files and report verification limits honestly.

## 4. Roles, Safety & Communication

- **Single-writer**: Codex edits project files; Gemini inspects, verifies as needed, and communicates. Do not run concurrent writers against the same or overlapping parent/child directories.
- **Git discipline**: Never automatically `git add`, `commit`, `push`, `merge`, `reset`, `clean`, or switch branches unless explicitly requested. Non-Git workspaces need no `git init` and keep plugin metadata outside the user's folder.
- **Honest verification**: `status: "completed"` means the Codex Turn finished, not that acceptance checks passed. Run necessary verification for code/deliverables and separate measured Codex usage from unavailable AGY usage.
- **Natural reporting**: Lead with completed behavior, actual checks, and any real blockers or limits. Keep Run IDs, Thread IDs, and log paths out of normal replies unless troubleshooting.

For Strict Workflow contracts, plans, handoff, or legacy Ticket recovery, load only the relevant file under `references/` (`host.md`, `planning.md`, `handoff.md`).
