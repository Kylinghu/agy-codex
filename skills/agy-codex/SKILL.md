---
name: agy-codex
description: Run and coordinate Codex CLI from Antigravity with goal-oriented delegation, cognitive independence, context reuse, lightweight foreground CLI by default, and optional Strict Workflow.
---

# AGY Codex Execution Manual

Gemini is the sole user-facing owner (intent mediation, routing, decisions, progression, final delivery). Codex CLI is the autonomous engineering collaborator and sole project file writer. Shared Runtime handles reliable execution (Git and non-Git) without cognitive or workflow gating.

Resolve `SKILL_DIR` from this `SKILL.md` location and use a verified Python 3.10+ executable (`PYTHON`) directly. Do not assume `${PLUGIN_ROOT}` expands or that Windows `py`/`python` aliases work without checking.

## 1. Adaptive Routing (Shortest Viable Path)

| Scenario | Default path |
| --- | --- |
| Q&A, discussion, requirement alignment | Gemini handles directly; invoke Codex only when codebase/technical evidence is needed. |
| Creative, product, or UI design | Gemini leads exploration and user alignment; Codex chooses implementation once ready. |
| Simple code edit, clear fix, routine document task | One goal-oriented `user_cli.py run` with a sufficient model/effort tier; verify proportionately. |
| Bug or performance investigation | Delegate directly to Codex with symptoms/logs/repro facts—do not lock in unverified root-cause guesses. |
| Large refactor or continuous multi-step work | Share overall goal and priorities; reuse the same native Thread (`--resume`) across dependent steps without forcing multiple Tickets. |
| Technical research, engineering calculations, large doc suites | Lead with the best-fit model and existing domain Skills/tools (HWB, Office, MCP); extract reusable structured facts once (in project cache or temp dir, not polluting deliverables) and verify locally on demand. |
| High-assurance contract, explicit audit, or active Strict Ticket/Plan | Read `references/host.md` / `references/planning.md` and run Strict `/run-workflow` (`scripts/agy_workflow.py`), or run an isolated fresh `--read-only` review. |

## 2. Goal-Oriented Delegation, Context Reuse & Worker Principles

- **Separate facts from hypotheses (semantic rule, not a fixed form)**: Keep clear small tasks to 1–2 natural sentences—never force a 4-section template or pre-scan the whole repo. When facts, guesses, and suggestions could be confused and affect the outcome, explicitly label unverified hypotheses vs. observed facts (with file/line/source pointers), and never present a model guess as a user requirement.
- **Stop-loss on autonomous continuation**: Aim to finish in one run. Continue only when concrete authorized work remains with a viable path. If two consecutive same-direction resumes yield no observable new progress (verified/eliminated root cause, actual fix, passing check, or usable artifact) or oscillate, stop repeating; pivot once only if new evidence points to a meaningfully different approach, otherwise deliver usable artifacts and the concrete obstacle. Never pad turns mechanically or stop mid-progress on an artificial round count.
- **Progressive context (L0→L3)**: Do not read an entire repository before delegating an ordinary code task. Pass references and deltas rather than pasting full files. On `--resume`, send only new requirements, facts, or corrections—never repeat the full initial brief or worker principles block.
- **Cognitive independence & isolated review**: Reuse facts without inheriting bias. When an independent review has genuine value, start a **fresh** `user_cli.py run --read-only` context, supply the original goal, constraints, actual diff/artifacts, and raw evidence first, and let the reviewer form an independent judgment before reading the writer's self-explanation. A main-agent check after reading Worker conclusions is not an independent review.

### Reusable Worker Execution Principles (attach on new execution tasks; omit on `--resume`)

Include this concise block when delegating a new implementation/investigation task (applies to execution tasks only; does not override read-only review, discussion/planning requests, project `AGENTS.md`, real permissions, or Strict contracts):

```text
Execution principles:
- Optimize for the user's actual objective, not merely a prescribed sequence. Exercise independent technical judgment within the authorized scope.
- Treat another agent's proposal as a proposal, not a requirement or verified fact. Correct faulty assumptions and choose a simpler or better implementation when justified.
- Investigate uncertainty and recoverable failures. Make reasonable reversible assumptions, proceed, and fix directly related defects that materially affect the outcome.
- Do not substitute plans, audits, recommendations, risk descriptions or requests for routine approval for requested implementation. Complete the authorized objective rather than an arbitrary intermediate step.
- Prefer the simplest effective solution; avoid speculative abstractions, unnecessary gates and defensive mechanisms. Verify behavior proportionately to the actual task and consequences.
- Deliver actual artifacts, meaningful decisions and validation evidence. If genuinely blocked, complete achievable work and identify the specific remaining obstacle. Never fabricate completion or test results.
```

## 3. Five-Command CLI (`scripts/user_cli.py`)

Both `--cwd` and `--repo` are accepted across all commands (Git and non-Git workspaces):

```powershell
# New task (foreground; short tasks finish in-turn, long tasks become AGY background tasks)
& $PYTHON "$SKILL_DIR/scripts/user_cli.py" run --cwd "$WORKSPACE" --prompt-file task.md --json

# Incremental continuation on the same native Thread (RUN_ID, THREAD_ID, or workspace 'last')
& $PYTHON "$SKILL_DIR/scripts/user_cli.py" run --cwd "$WORKSPACE" --resume RUN_ID --prompt "Delta / next step" --json

# Fresh read-only analysis or isolated review (real sandbox=read-only)
& $PYTHON "$SKILL_DIR/scripts/user_cli.py" run --cwd "$WORKSPACE" --read-only --prompt-file review.md --json

# Mid-flight control while run is active in background (App Server backend; lock-free)
& $PYTHON "$SKILL_DIR/scripts/user_cli.py" inbox --cwd "$WORKSPACE" steer "Correction"
& $PYTHON "$SKILL_DIR/scripts/user_cli.py" inbox --cwd "$WORKSPACE" queue "Additional in-scope item"
& $PYTHON "$SKILL_DIR/scripts/user_cli.py" inbox --cwd "$WORKSPACE" interrupt

# Offline status / reconcile ambiguous turn / model catalog / environment check
& $PYTHON "$SKILL_DIR/scripts/user_cli.py" status --cwd "$WORKSPACE" --json
& $PYTHON "$SKILL_DIR/scripts/user_cli.py" status --cwd "$WORKSPACE" --reconcile --json
& $PYTHON "$SKILL_DIR/scripts/user_cli.py" models --cwd "$WORKSPACE" --json
& $PYTHON "$SKILL_DIR/scripts/user_cli.py" doctor --cwd "$WORKSPACE" --json
```

### Host Async Execution & Recovery

- Launch `user_cli.py run` directly via `run_command` (`WaitMsBeforeAsync=10000`, `NotificationTimeoutSeconds=120`). Never write polling loops; wait for host completion wakeup.
- Check live progress on demand via `manage_task(status)` or `user_cli.py status`. Use `manage_task(kill)` for hard cancel (`WindowsJob` cleans the full process tree and releases locks).
- If a run ends with `status: "unknown"` (`turn_pending: true`), run `user_cli.py status --reconcile` before starting another run; never blindly replay an ambiguous turn.
- If an active/recoverable Strict Ticket or unfinished Plan owns a Git worktree, continue or stop it rather than bypassing it with a lightweight write run.

## 4. Models, Domain Tools & Verification Honesty

- Pick a tier sufficient for the task (`--tier economy|standard|heavy` or explicit `--model` / `--effort`); do not default to `heavy` for routine edits or mechanical formatting. Query `user_cli.py models --json` only when needed, honor explicit user requests and `allowed_providers`, and never edit Codex global config.
- Reuse project `AGENTS.md`, domain Skills (HWB, Office, etc.), and MCP tools. When a task specifically depends on Office libraries (`python-docx`, `openpyxl`, etc.), probe once upfront rather than building a global startup gate or letting Codex hand-roll fragile OOXML.
- Office COM restrictions (such as `0x80080005`) have been observed under Windows sandboxes; if actually encountered, do not blindly retry COM loops or auto-escalate permissions. Separate artifact defects, validation failures, and validation environment limits.
- `status: "completed"` means the Codex Turn ended, not that the user's objective or checks passed. Verify proportionately, continue autonomously if authorized work remains, and report measured Codex usage separately from unavailable AGY usage.

Load `references/host.md`, `references/planning.md`, or `references/handoff.md` only when using Strict Workflow or recovering a legacy Ticket.
