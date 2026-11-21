# Changelog

## 0.1.1 — 2026-11-21

### Fixed

- Match task scope from the repository root, with `**` spanning zero or more
  directories and ordinary wildcards staying within one path component.
- Include explicitly named ignored files in task baselines and review snapshots,
  so their changes invalidate stale evidence. Keep ignored dependency and build
  directories outside the default snapshot.
- Restore recorded user decisions and environment resolutions when a reviewer is
  recreated after later revisions, including older saved review packets.
- Include the project's `LICENSE` in Python-installer copies.

### Validation and documentation

- Run the offline test suite in GitHub Actions on Ubuntu and Windows with
  Python 3.10 and 3.12.
- Add regression coverage for task scope, ignored-file evidence, reviewer
  recovery, and installed license contents.
- Keep test fixtures portable across Windows temporary-path aliases and
  non-UTF-8 process encodings.
- Clarify the Python interpreter used by Antigravity workflows and place relevant
  final integration checks in plans that change coupled behavior.

## 0.1.0 — 2026-11-21

- Initial public release of the Antigravity plugin, serial Codex controller,
  persistent task and plan state, read-only review loop, and offline test suite.
