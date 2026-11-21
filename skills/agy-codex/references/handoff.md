# Persistent handoff and recovery

Adapt Matt Pocock's handoff method: point to existing artifacts and add only context
that is not already recorded. Keep authoritative engineering decisions in the
project's PLAN.md, and relevant unrecorded progress in RESUME.md when that path is
in scope. Do not use the OS temporary directory for recoverable task state.

The controller stores the task contract, exact Codex Session mapping, snapshots,
test evidence, review and JSONL summaries under this worktree's Git management
directory. Use the saved current task for “continue”; never guess with --last.

When an active plan exists, continue its first unfinished Ticket and then the
remaining ordered tasks. plan.json stores frozen contracts and Ticket mappings;
Ticket states decide the completed prefix, not the cached cursor alone. A crash
after PASS does not repeat implementation. Only the final/current snapshot is
compared to the latest worktree; earlier PASS records belong to their own steps.
Status reads this saved state without creating the next Ticket.

Rebuild only what is needed: task title and plan pointer, completed outcome,
unresolved findings, next action, evidence pointers, and relevant project Skills.
Do not rewrite a full handoff after every step. A repair in the same Session
receives unresolved findings and new evidence; unchanged task context is referenced.

Continue handles a pending review without rerunning implementation. A recorded
timeout/cancellation is recoverable after verifying the owned process is stopped.
Three exhausted attempts, missing Session identity, remain hard escalations; do not invent a new task to hide a fourth attempt.

A saved PASS applies to its recorded snapshot. If files subsequently changed,
report that fact rather than presenting the earlier checks as current verification.
Resume requires the same machine, worktree and Codex user directory. The task
files do not contain a copy of Codex's entire session store.

Canceling a workflow preserves the plan for recovery. An explicitly stopped plan
stays stopped; do not silently restart it. Live summaries are in progress.json,
with updated_at and elapsed time. After an abnormal host exit they can be stale;
check the owned process during recovery rather than assuming running=true is live.


A concrete blocker may have resolution_allowed=true. Read its actual question,
category, blocked round and evidence before resolving. Preserve the original Session
and remaining implementation budget; do not treat a plain “continue” as an answer.
A result returned to the main Agent is factual input for natural communication, not
text that must be relayed verbatim.
