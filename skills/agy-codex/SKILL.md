---
name: agy-codex
description: Execute or continue a scoped task or agreed ordered plan with the AGY Codex Antigravity plugin, where Codex CLI writes, Gemini reviews and communicates. Use for AGY Codex requests, AGY directing Codex, plan execution, recovery, or workflow progress.
---

# AGY Codex execution manual

This Skill is the native plugin's internal work manual. The selected AGY Codex main
Agent owns user communication throughout the conversation. It can also be invoked
as `/agy-codex` in another conversation; that invocation alone does not change the
whole conversation's main Agent or model.

Reuse the user's project Skills and documents. Adapted Matt Pocock methods are
bundled; installing his entire suite is not required.

## Route intent

| User intent | Action |
| --- | --- |
| Implement/fix a goal | Read `references/planning.md`, prepare one scoped task, then start the saved workflow. |
| Execute an existing plan | Read the requested sections; prepare a finite ordered plan using existing decisions, dependencies and acceptance. |
| Continue | Prefer the saved plan; resume its current task with the original Session and remaining attempts, then advance. |
| Status | Run `scripts/user_cli.py --repo <workspace> status --json`; no Worker or reviewer. |
| Answer a pending question | Inspect blocker and resolution_allowed; use resolve with actual answer/source and blocked round. |
| End the plan | After an explicit user instruction, use stop_plan with the stated reason; preserve all evidence. |

Resolve paths from the SKILL.md being read, not from assumed template variables.
Use the workspace named by the user or containing the named plan. Ask only if the
workspace remains ambiguous after inspection. Before new work inspect status:
resume a matching unfinished task; do not silently replace its goal. Repeated
identical start is idempotent. A fresh goal does not reuse an unrelated Session.

## Execute

Read `references/host.md` for the internal call contract. Pass explicitly requested Codex model and reasoning depth in the optional `codex` workflow object; preserve saved settings on ordinary continue. Do not modify Codex's global config. Invoke AGY's existing
`/run-workflow` capability with saved `scripts/agy_workflow.py`; do not regenerate
it or ask the user to author JSON, supply IDs, or copy handoff prompts. This is a
host script using injected globals, not a standalone Python command.

Codex performs implementation; declared checks run independently; one read-only
reviewer (inheriting the selected conversation model; choose Gemini) checks Spec and Standards. An in-scope engineering choice is
resolved by that reviewer as an actionable REVISE. Genuine user choices or observed
permission blockers remain explicit ESCALATE records. If resolution_allowed is true,
record the actual answer or verified environment fix before continuing the same
Session; resolution grants no additional execution permissions.

At most three implementations per task. Read-only review output correction retries
once without another Worker call. Do not create a replacement task to hide a fourth
attempt. Retain the genuine reason when a permission, Session or process check fails.

For context recovery read `references/handoff.md`. Load other references only at
the relevant stage; execution and review rules are injected automatically. Upstream
attribution and adaptation decisions are in `references/upstream.md`.

## Roles and existing work

- AGY plans and reviews read-only. Codex alone edits project files, including PLAN.md
  and RESUME.md when in scope. Put unmaterialized plan notes in the internal contract.
- The controller writes local metadata and runs declared checks. No concurrent writer
  in this worktree; the OS lock coordinates this tool, not arbitrary external editors.
- Preserve existing uncommitted/staged work. The task-start baseline captures scoped
  file bytes; review uses only changes since that point. A clean worktree is not required.
- Reuse AGENTS.md and relevant existing engineering Skills. Adapt their orchestration
  to these roles; do not overwrite them or spawn duplicate writer/reviewer loops.
- No automatic stage/commit/push/merge/reset/clean, branch switch or recursive AGY Codex.

## Explain results

Use report fields as facts; the CLI's fixed message is only a fallback. Lead with
completed behavior and actual verification. For a true decision, ask the specific
question with a recommendation. For a recoverable issue, investigate and proceed
within existing authority. Do not ask for routine engineering approvals.

Keep technical IDs/logs in diagnostics. Never turn a stale snapshot, unresolved
Worker issue or missing evidence into a claimed PASS. Worker checks are self-reports;
tests in the review packet are independently executed. Report measured Codex usage
separately from unavailable AGY usage; character counts are not tokens.

A workflow handles one task or a finite, already-agreed ordered plan. For a plan,
each PASS starts the next task from the current uncommitted baseline. A blocker,
unfinished review or stale result stops progression; never skip work. Each task
has its own Session and reviewer; repairs retain that task's three-round budget.
Do not ask the user to manage JSON or IDs, or approve an already-authorized next step.

An active host workflow is required for continuous execution. Canceling pauses
the current run; a later continue recovers the saved position. stop_plan explicitly
ends the plan and ordinary continue does not reopen it. It cannot interrupt a
running workflow concurrently: cancel that run and wait for cleanup first.

Default to phases and results (progress=stages); detailed counters remain on disk.
Read status once when the user asks for progress; do not create a polling loop or
periodic model turns. Use progress=detailed only for requested detail/diagnostics.
It affects this workflow invocation's display, not the frozen contract or Session.
Both modes omit timed heartbeats; detailed updates require real changes and have
a per-round limit. The first validation update and final result remain visible.

Do not relay raw JSONL, reasoning text or complete command output, and do not
invent percentages. Saved progress is the latest observation, not proof of current
liveness; check its timestamp. Local summarization makes no model call, but AGY
logs still enter the workflow result.
