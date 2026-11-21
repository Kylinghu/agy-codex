# Planning and task slicing

Adapted from Matt Pocock's to-spec, to-tickets and writing-for-agents methods.

Start from the user's goal, current repository, and existing project documents.
Prefer the user's planning-with-files-zh or equivalent planning conventions when
available. Read the relevant PLAN.md section and RESUME.md; keep their decisions.
Do not produce a second spec that restates an adequate existing plan.

For a new goal, determine the user-visible outcome, scope, important decisions,
acceptance evidence, and observable test interfaces. Reuse project patterns and
real test commands. Already agreed scope and test interfaces are sufficient;
ask only about a missing decision that changes the implementation or acceptance.

Select one coherent behavior that can be verified on its own. Split larger work
along independently verifiable outcomes, not an arbitrary layer-per-ticket list.
For broad mechanical migrations, preserve working behavior between steps. Identify
blockers from the plan; do not claim them complete without evidence. A selected
task uses start/task. A finite group already requested by the user uses start/plan
with ordered step keys and complete task contracts. Do not add unrequested work.
Keep the user's order; dependencies may refer to earlier keys or existing passed
Ticket IDs. Do not dispatch future steps concurrently or reorder a dependency graph.

Prepare the internal task object from references/host.md. Keep title and goal
short. Acceptance is the frozen contract for this task. Use plan_ref for the
canonical plan and task ID/heading; the controller records its starting hash.
The plan itself is not copied into every prompt. Only include plan_notes when new
decisions do not exist in a project artifact yet. Codex can materialize these notes
in PLAN.md/RESUME.md if those files are explicitly in allowed_paths.

For an ordered plan, freeze all task contracts before execution. Their source
pointer remains a reference; progress edits to PLAN.md do not silently change
acceptance. Next-task baselines are captured only after the previous PASS, so
existing results become the new starting point without an intermediary commit.
If the user changes scope, preserve the old plan's evidence and form the new
agreed scope instead of replacing contracts in place.

When several steps change coupled behavior, include an appropriate final
integration/regression task in the agreed plan, or make those checks part of its
last task. Use the project's existing check commands to verify the combined result
and the earlier behavior affected by later steps. Define this acceptance when
preparing the plan so it runs automatically within the same workflow.

Choose practical allowed_paths and deterministic argv test_commands from the project.
Supports exact relative files, directory prefixes (e.g. `src/` or `src`), glob
patterns (e.g. `tests/test_*.py` or `**/*.ts`), and `.` for repo-wide refactors.
Patterns are anchored at the repository root: ordinary wildcards stay within a
path component, while `**` spans zero or more directories. Name an ignored target
file explicitly, including before creation; directory prefixes and globs do not
expand ignored dependency or build trees.
Avoid overly brittle file-by-file lists when a task may touch helper or test
files in a module; prefer module directories or globs so Codex is not falsely
escalated for touching an in-scope helper. Keep independent acceptance fixtures
outside the editable scope where practical; Worker-authored unit tests can be in
scope. For a behavior change, the accepted test seam must observe externally
meaningful behavior, not reproduce internals. Do not start a task with invented
commands or no observable acceptance evidence.

Before dispatch, tell the user in one or two sentences what will be delivered and
how it will be checked. Continue when the work is already authorized. Do not add
routine approval rounds for a plan the user has already accepted.
