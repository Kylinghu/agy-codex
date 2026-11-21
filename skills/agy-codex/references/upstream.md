# Upstream methods and local adaptations

This is an independent AGY Codex adaptation, not an official Matt Pocock product.
It bundles adapted instructions so users do not need to install or invoke the
entire upstream suite. Runtime behavior does not fetch upstream content.

Source: https://github.com/mattpocock/skills

Reference revision recorded in the preceding source review:
`abeeaf95660ba9e8b7c753eb38f441ca707a8823` (2026-11-19).
The current upstream skill bodies and MIT notice were also read on 2026-11-21.
This package contains rewritten/adapted contracts, not a byte-identical vendor copy.

| Upstream path under skills/ | Integration in this package | Deliberate adaptation |
| --- | --- | --- |
| engineering/to-spec/SKILL.md | planning.md: synthesize existing decisions into an actionable goal | Reuse local PLAN.md; no external tracker prerequisite or compulsory long story list. |
| engineering/to-tickets/SKILL.md | planning.md and internal contract: verifiable behavior slices, dependencies, acceptance | Execute one selected task; no task platform or unattended graph scheduler. |
| engineering/implement/SKILL.md | execution.md, injected on the first Codex turn | AGY owns review; leave changes uncommitted and omit original automatic code-review/commit. |
| engineering/tdd/SKILL.md | execution.md: behavior at agreed public interfaces and independently derived expected results | Reuse approved interfaces and existing project tdd; no repeated approval rounds. |
| engineering/code-review/SKILL.md | review.md and response axes.spec/axes.standards | One read-only reviewer checks both axes; use the captured working-tree diff including untracked files. |
| productivity/handoff/SKILL.md | handoff.md and repair-delta prompts | Persist through project documents and controller files, not OS temporary storage. |
| productivity/writing-for-agents/SKILL.md | Short entry, stage-specific references, plan pointers | Reduce duplicate context; do not claim a measured token saving without model usage evidence. |
| engineering/implement-spec/SKILL.md | Planning dependencies and sparse reference-based communication only | Do not import concurrent worktrees, branch orchestration, reset, merge, or PR automation. |

Repository-specific Skills remain intact. They supply conventions and domain
methods; the explicit AGY Codex task owns its writer, Session, review and Git
policy. The full upstream permission notice is in mattpocock-license.txt beside
this file and travels with installed copies.
