#!/usr/bin/env python3
"""Create an isolated real Ticket; optionally verify actual Codex write + resume."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from controller import Controller, WorkflowError, atomic_json, brief, doctor
from plans import load_plan, plan_report, start_plan


def initial_commit(repo):
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    # A local fixture commit only; never called against an existing directory.
    hooks = repo/".git"/"empty-hooks"; hooks.mkdir()
    subprocess.run(["git", "-C", str(repo), "add", "--", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=AGY Codex Smoke", "-c", "user.email=smoke@localhost",
                    "-c", "core.hooksPath="+str(hooks), "-c", "commit.gpgsign=false", "commit", "-m", "Initialize isolated smoke fixture"],
                   check=True, capture_output=True)


def setup(repo, probe=False, plan_mode=False):
    repo = Path(repo).resolve()
    if repo.exists():
        raise WorkflowError("Choose a new directory for the isolated fixture. Existing files will not be overwritten.")
    repo.mkdir(parents=True)
    (repo/".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    if probe:
        (repo/"README.md").write_text("Isolated Codex execution and session restoration capability probe.\n", encoding="utf-8")
        spec = {
            "id": "PROBE-001", "title": "Check real Codex write and resume",
            "goal": "This is an intentionally two-stage protocol probe. On the FIRST invocation, create probe.txt with exactly STARTED followed by a newline, and stop. The final acceptance test is expected to fail at this stage. On the explicit FOLLOW-UP asking for stage two, append RESUMED and a newline. Change only probe.txt. Do not skip the staged protocol.",
            "acceptance": ["The first invocation writes STARTED\\n.", "The explicit resumed invocation appends RESUMED\\n in the same Session."],
            "allowed_paths": ["probe.txt"],
            "test_commands": [["{python}", "-c", "from pathlib import Path; assert Path('probe.txt').read_text() == 'STARTED\\nRESUMED\\n'"]],
            "max_rounds": 3,
        }
    else:
        (repo/"clamp.py").write_text('''def clamp(value, lower, upper):
    """Clamp value to an inclusive range; reject reversed bounds."""
    # Deliberately incomplete starting implementation for the independent Ticket.
    return max(value, lower)
''', encoding="utf-8")
        (repo/"test_clamp.py").write_text('''import unittest
from clamp import clamp

class ClampTests(unittest.TestCase):
    def test_below(self): self.assertEqual(clamp(-3, 0, 10), 0)
    def test_inside(self): self.assertEqual(clamp(3, 0, 10), 3)
    def test_above(self): self.assertEqual(clamp(20, 0, 10), 10)
    def test_equal_bounds(self): self.assertEqual(clamp(20, 4, 4), 4)
    def test_float(self): self.assertEqual(clamp(0.5, -1.0, 1.0), 0.5)
    def test_reversed(self):
        with self.assertRaises(ValueError): clamp(3, 10, 0)

if __name__ == '__main__': unittest.main()
''', encoding="utf-8")
        (repo/"PLAN.md").write_text("# Plan\n\nImplement clamp according to its Ticket; preserve the acceptance tests.\n", encoding="utf-8")
        (repo/"RESUME.md").write_text("# Resume\n\nThis is an isolated fixture. No implementation has run yet.\n", encoding="utf-8")
        spec = {
            "id": "SMOKE-001", "title": "Implement inclusive numeric clamp",
            "goal": "Fix clamp.py so clamp(value, lower, upper) clamps to the inclusive range and raises ValueError when lower > upper. Preserve the acceptance tests. Update the existing PLAN.md and RESUME.md briefly with actual changes and validation.",
            "acceptance": ["Below-range input returns lower; above-range input returns upper; in-range values are unchanged.",
                           "Equal bounds are supported and reversed bounds raise ValueError.",
                           "Integer and finite float inputs work; original tests are preserved."],
            "allowed_paths": ["clamp.py", "PLAN.md", "RESUME.md"],
            "test_commands": [["{python}", "-m", "unittest", "-v", "test_clamp"]], "max_rounds": 3,
        }
    if plan_mode:
        (repo/"clamp_many.py").write_text('''from clamp import clamp

def clamp_many(values, lower, upper):
    """Return a new list of individually clamped values without changing input."""
    return list(values)
''', encoding="utf-8")
        (repo/"test_clamp_many.py").write_text('''import unittest
from clamp_many import clamp_many

class ClampManyTests(unittest.TestCase):
    def test_mixed(self): self.assertEqual(clamp_many([-2, 3, 12], 0, 10), [0, 3, 10])
    def test_empty(self): self.assertEqual(clamp_many([], 0, 10), [])
    def test_tuple(self): self.assertEqual(clamp_many((-2, 12), 0, 10), [0, 10])
    def test_input_unchanged(self):
        values = [-2, 12]
        result = clamp_many(values, 0, 10)
        self.assertEqual(values, [-2, 12])
        self.assertIsNot(result, values)

if __name__ == '__main__': unittest.main()
''', encoding="utf-8")
        (repo/"PLAN.md").write_text(
            "# Agreed smoke plan\n\n## T-01\nImplement numeric clamp and preserve its six acceptance tests.\n\n"
            "## T-02\nUse clamp to implement clamp_many, returning a new list without changing its input. "
            "Preserve all ten acceptance tests. Update this plan and RESUME.md with observed results.\n", encoding="utf-8")
        spec.pop("id")
        second = {"title": "Apply clamp to a collection", "goal": "Implement clamp_many.py by reusing clamp. Return a new list of clamped values for a list or tuple; keep the input unchanged and support an empty collection. Preserve the acceptance tests and briefly update PLAN.md and RESUME.md.",
                  "acceptance": ["Every item is clamped using the first task's clamp function.",
                                 "Empty input returns an empty list; list/tuple inputs are not changed.",
                                 "All original ten tests pass unchanged."],
                  "allowed_paths": ["clamp_many.py", "PLAN.md", "RESUME.md"],
                  "test_commands": [["{python}", "-m", "unittest", "-v", "test_clamp", "test_clamp_many"]],
                  "depends_on": ["T-01"]}
        value = {"title": "Two-step AGY Codex smoke plan", "plan_ref": {"path": "PLAN.md"},
                 "tasks": [{"key": "T-01", "task": spec}, {"key": "T-02", "task": second}]}
    initial_commit(repo)
    c = Controller(repo)
    state = start_plan(c, value) if plan_mode else c.create(spec)
    return c, state


def probe(repo):
    c, state = setup(repo, probe=True)
    result = {"type": "REAL_CODEX_CAPABILITY_PROBE", "agy_review": "NOT_RUN", "worktree": str(c.repo),
              "doctor": doctor(c.repo), "write": False, "same_session_resume": False}
    if result["doctor"]["static_checks_passed"]:
        first = c.run(state["id"])
        result["first"] = brief(first)
        path = c.repo/"probe.txt"
        result["write"] = first["status"] == "REVIEW" and path.is_file() and path.read_text() == "STARTED\n"
        if result["write"]:
            packet = c.packet(state["id"])
            verdict = {k: packet[k] for k in ("ticket_id", "round", "snapshot_sha256")}
            verdict.update(axes={"spec": "Stage one of the declared probe", "standards": "Isolated capability probe"}, blocker=None, verdict="REVISE", summary="Stage one completed. Execute the intentionally deferred second stage now.",
                           findings=[{"id":"PROBE-STAGE-2", "path":"probe.txt", "reason":"The two-stage capability probe has not run stage two.",
                                      "requested_fix":"Append RESUMED followed by a newline; preserve STARTED. This is the explicit stage-two follow-up."}])
            c.verdict(state["id"], verdict, source="capability-probe-controller")
            second = c.run(state["id"])
            result["second"] = brief(second)
            result["same_session_resume"] = second["status"] == "REVIEW" and second["session_id"] == first["session_id"] and path.read_text() == "STARTED\nRESUMED\n"
            if result["same_session_resume"]:
                packet = c.packet(state["id"])
                verdict = {k: packet[k] for k in ("ticket_id", "round", "snapshot_sha256")}
                verdict.update(axes={"spec": "Both declared probe stages verified", "standards": "Isolated capability probe"}, blocker=None, verdict="PASS", summary="Real CLI write and exact Session resume verified by file contents and exit codes; AGY was not involved.", findings=[])
                c.verdict(state["id"], verdict, source="capability-probe-controller")
    result["passed"] = result["write"] and result["same_session_resume"]
    atomic_json(c.root/"live-probe.json", result)
    return result


def verify(repo, ticket="SMOKE-001"):
    c = Controller(repo); state = c.load(ticket)
    review = state.get("last_review") or {}
    result = {"ticket": brief(state), "agy_review": review.get("reviewer_source") == "agy-research",
              "snapshot_unchanged": c.snapshot_for(state)["sha256"] == state["expected_sha256"]}
    result["passed"] = state["status"] == "PASS" and result["agy_review"] and result["snapshot_unchanged"]
    # A recorded reviewer source is workflow provenance, not cryptographic host attestation.
    atomic_json(c.root/"smoke-result.json", result)
    return result


def verify_plan(repo):
    c = Controller(repo)
    plan = load_plan(c)
    if not plan:
        raise WorkflowError("No saved smoke plan was found.")
    summary = plan_report(c, plan)
    reviews = [bool((c.load(row["ticket_id"]).get("last_review") or {}).get("reviewer_source") == "agy-research")
               if row["status"] != "PENDING" else False for row in summary["tasks"]]
    result = {"plan": summary, "agy_review_recorded_per_task": reviews,
              "passed": summary["verified"] and all(reviews),
              "live_progress": "Check the real AGY UI while the workflow is active; saved logs alone are not host attestation."}
    atomic_json(c.root / "smoke-plan-result.json", result)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["setup", "setup-plan", "probe", "verify", "verify-plan"])
    p.add_argument("--repo", required=True)
    args = p.parse_args()
    try:
        if args.command in {"setup", "setup-plan"}:
            c, state = setup(args.repo, plan_mode=args.command == "setup-plan")
            result = {"plan": plan_report(c, state), "state_directory": str(c.root),
                      "next": "Open this repository in AGY Codex and continue the saved two-step plan."} if args.command == "setup-plan" else {
                      "ticket": brief(state), "state_directory": str(c.root), "next": "Run doctor, then use AGY /run-workflow with ticket SMOKE-001."}
        elif args.command == "probe":
            result = probe(args.repo)
        elif args.command == "verify-plan":
            result = verify_plan(args.repo)
        else:
            result = verify(args.repo)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("passed", True) else 2
    except (WorkflowError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
