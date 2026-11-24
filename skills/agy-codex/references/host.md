# Internal AGY invocation

This is for the Router, not a form the user must fill. Resolve SKILL_DIR to the
directory containing the bundled SKILL.md, and REPO to the selected workspace.
For ordinary CLI scripts, resolve one working Python 3.10+ interpreter and reuse
its exact `sys.executable` path. In the workflow this is the host's `sys.executable`;
outside it, use the configured interpreter or one already verified on the machine.
Do not assume Windows has `py`, or that a PATH `python` WindowsApps alias is usable.
Verify a candidate with `-c 'import sys; print(sys.executable); print(sys.version)'`.
Reuse `doctor.python_executable` after diagnosis; preserve virtual-environment paths.
The examples below use PYTHON for this verified executable, not a literal command.
In PowerShell use `& $agyCodexPython` with the path quoted/stored as one argument.
The AGY host script uses injected globals and top-level await; run it only through
the existing /run-workflow capability.

## Status

Run `PYTHON SKILL_DIR/scripts/user_cli.py --repo REPO status`. Add `--json` only when
structured diagnostic data is needed. This reads state and Git snapshots; it does
not invoke a model, run tests, or write project files.

## Start one task

Ask /run-workflow to execute the saved `SKILL_DIR/scripts/agy_workflow.py`, with args:

```json
{
  "controller_dir": "SKILL_DIR/scripts",
  "repo": "REPO",
  "action": "start",
  "task": {
    "title": "One concise task title",
    "goal": "The user-visible outcome",
    "acceptance": ["Observable criterion"],
    "allowed_paths": ["src/feature.py", "tests/test_feature.py", "RESUME.md"],
    "test_commands": [["{python}", "-m", "unittest", "tests.test_feature"]],
    "plan_ref": {"path": "PLAN.md", "section": "T-03"}
  }
}
```

Substitute real project paths/commands; the example is not a default test command.
Omit plan_ref for a genuinely new goal without a plan. Use optional plan_notes
for new decisions that Codex should materialize. Optional depends_on lists existing
controller task IDs whose saved result must be PASS; do not guess IDs from titles.
Task ID, request fingerprint and plan revision hash are generated automatically.
An identical start at the same HEAD reuses its task rather than implementing twice.

The worktree needs an existing HEAD; it need not be clean. Scoped task-start bytes
are saved before execution, preserving existing uncommitted/staged changes. No
commit/reset/clean is needed to satisfy the tool. Run only one writer per worktree.
The plugin install directory stays separate from each repository's task records.

## Start an agreed ordered plan

Use the same saved script and action start, with plan instead of task. The Router
prepares these contracts from the user's selected scope; this is not a user form.
For example (replace paths, criteria and commands with the real project values):

```json
{
  "controller_dir": "SKILL_DIR/scripts",
  "repo": "REPO",
  "action": "start",
  "plan": {
    "title": "Complete the agreed configuration import changes",
    "plan_ref": {"path": "PLAN.md"},
    "tasks": [
      {
        "key": "T-01",
        "task": {
          "title": "Read compatible configuration formats",
          "goal": "Implement the format handling already agreed in T-01.",
          "acceptance": ["Old and new formats both load with the agreed defaults"],
          "allowed_paths": ["src/config.py", "tests/test_config.py"],
          "test_commands": [["{python}", "-m", "unittest", "tests.test_config"]]
        }
      },
      {
        "key": "T-02",
        "task": {
          "title": "Use the compatible reader in the importer",
          "goal": "Implement the importer behavior already agreed in T-02.",
          "acceptance": ["The importer accepts both formats and preserves the agreed behavior"],
          "allowed_paths": ["src/importer.py", "tests/test_importer.py"],
          "test_commands": [["{python}", "-m", "unittest", "tests.test_importer"]],
          "depends_on": ["T-01"]
        }
      }
    ]
  }
}
```

Step keys must be unique. The list determines order; dependencies only name earlier
step keys or existing PASS Ticket IDs. Each task inherits the outer plan_ref path
with its key as section unless it has an explicit pointer. Plan/Ticket IDs are
generated automatically. Identical start is idempotent; it never resets attempts.
Each task retains its own Session/reviewer; PASS advances inside this workflow.
An unresolved blocker, incomplete review or changed snapshot stops progression.

## Continue

Run the same saved workflow with args containing controller_dir, repo, and
`"action": "continue"`. It resolves the active plan and its current task, or the
single active task if no plan exists. Optional plan_id selects a saved plan;
ticket selects a single saved task, but cannot skip the current unfinished plan.
The legacy `ticket`-only invocation remains supported. `resume` is an alias for
continue. Do not add task/plan to a continue call or change frozen acceptance.

The workflow returns a concise report and logs phase messages. For an environment
problem, explain the failed checks in the user's language; use the saved doctor
details when diagnosing. Do not paste the full JSON report into the conversation.

If a pending review has a stale snapshot, inspect the external changes first.
When they are within the existing authorized scope and should be reviewed, run
`PYTHON SKILL_DIR/scripts/controller.py --repo REPO refresh TASK_ID`, then continue.
This reruns declared validation and regenerates review evidence, not implementation.
Do not use refresh to absorb an unexplained or out-of-scope change.

## Progress and ending a plan

Use the top-level workflow option progress with stages (default) or detailed.
Omit it for normal work. It is a display preference for this invocation; keep it
outside task/plan contracts, request fingerprints and Session identity.

The adapter calls AGY phase/log from the event-loop thread while the controller
runs in its executor. Default stages shows phases, the first validation update,
and results. It does not stream command/file counters. Detailed mode adds only
changed observed counters/file names, at least 5 seconds apart and at most 12
detail messages per run_controller call. First validation and completion bypass
that limit; no mode sends timed heartbeat logs. Very short stages may coalesce.
No additional model is called; log messages still enter AGY's workflow result.

Status reads plan totals and recent progress without starting a Worker/reviewer.
progress.json is refreshed about every 2 seconds and on stages/logs/completion;
updated_at identifies its observation time. A hard host exit can leave old records.
Read it once when asked; never add a model-driven polling loop. To enable detail
for a requested diagnostic run, pass for example:

```json
{"controller_dir": "SKILL_DIR/scripts", "repo": "REPO", "action": "continue", "progress": "detailed"}
```

Canceling the active AGY workflow requests subprocess cleanup and preserves the
plan. To explicitly end a paused plan, use the same workflow with action stop_plan
and reason containing the user's stated reason. It returns STOPPED, preserves
results, and will not restart on continue. This is not a concurrent interrupt API:
the workflow lock spans execution and review; cancel/wait before stop_plan.
The engine has no daemon to execute while the host workflow is closed.

## Independent smoke test

For first-use validation, create a new separate directory with
`PYTHON SKILL_DIR/scripts/smoke.py setup --repo NEW_REPO`. In AGY, open NEW_REPO and
continue the existing SMOKE-001 task. Run the same smoke.py with `verify --repo
NEW_REPO` after completion. The separate `probe --repo ANOTHER_NEW_REPO` command
checks real Codex write/resume capability; it is not an AGY review test.

For the ordered-plan path use setup-plan --repo NEW_PLAN_REPO, then ask AGY to continue
the saved two-step plan. It should run both steps in one workflow. verify-plan
checks recorded per-task reviews and the final snapshot. Observe real UI progress
and, if practical, cancel/continue once; saved source labels do not attest the host.


## Resolve a concrete blocker

The main Agent reads status/report first (current within a plan result). If resolution_allowed is true, the saved
blocker and round identify the pending question. Use the same workflow with:

```json
{
  "controller_dir": "SKILL_DIR/scripts",
  "repo": "REPO",
  "action": "resolve",
  "resolution": {
    "round": 1,
    "answer": "The actual answer within the unchanged acceptance contract",
    "source": "user",
    "evidence": "Actual observed environment fix, or relevant context for the answer"
  }
}
```

Use the CURRENT reported round, not the sample. A product_decision requires the
user's actual answer. Environment/permission blockers require concrete fix evidence;
source router means the Router observed that fix, not that the user approved it.
The original sandbox and approval settings remain unchanged, and preflight runs
again before execution. No permissions are granted by this JSON. A scope change
requires a new agreed task, not a mutated recovery contract. Session mismatch,
unknown processes and exhausted rounds cannot be resolved this way. Duplicate or
stale resolution is rejected, not replayed. To abandon a saved task after an
explicit user instruction, use controller.py escalate TASK_ID --reason TEXT; do
not erase its state. Unresolved/recoverable tasks keep the worktree reserved.

## Worker model overrides

Optional top-level `codex` object accepts `model` (exact CLI model ID) and
`reasoning_effort` (exact CLI effort). On start, these explicit settings override
individual task/plan settings and are saved in each task contract. On continue,
apply only an explicitly requested override to subsequent Worker calls of the
current Ticket; the same Session and remaining round budget are retained. When a
plan advances, its later Tickets retain their original settings. Never rewrite the task
acceptance or create another Ticket simply to upgrade the Worker. Per-task
`codex` and plan-level `codex` may be used for different steps. Omit unknown
settings; do not invent model IDs or unsupported efforts. These are requested
settings, not a server-verified model identity. No changes to CODEX_HOME or
Codex Desktop defaults are needed.
