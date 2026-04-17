# music-dj-tools v1.0.1 — Patch Release

**Release date:** 2026-04-17
**Previous:** [v1.0](RELEASE-NOTES-v1.0.md) (`ecbd31d`, 2026-04-17)
**Theme:** Safety rails, robustness, forensics/diagnose remediation, documentation.

This patch rolls up ~30 PRs merged on top of v1.0. It is a drop-in upgrade —
no user-facing API changes, no breaking changes. All improvements land behind
existing contracts; test count rises to **2327 passed, 45 skipped, 0 failing**.

---

## Highlights

### Safety rails (P1 / P2 write-path hardening)
- **#110 — P1-A `apply_plan` self-guard.** Refuses to rewrite the source library
  in place; requires an explicit destination distinct from the input path.
- **#111 — P1-B `DjayPlaylistWriter` self-guard.** Same invariant applied to the
  djay playlist writer: no in-place overwrite of the live library.
- **#116 — P2 djay-db write-path contract.** Every djay-db writer now routes
  through a single validated contract that refuses to touch a live/attached
  database file; enforced at construction time with dedicated tests.

### Robustness
- **#113 — Voice daemon optional `sounddevice`.** Missing optional audio
  dependency no longer crashes the daemon; it degrades gracefully and exits
  with `rc=0` while logging a clear actionable message.
- **#124 — TSAF constant startup validation.** Track-Source-Authority-Field
  constants are validated at import time so a malformed config fails loudly at
  startup rather than mid-run.
- **#115 — Sleep → event-wait test patterns.** Flaky `time.sleep()`-based
  synchronisation in the async test suite was replaced with deterministic
  event waits, stabilising CI and cutting suite wall-clock.

### Refactor
- **#120 — `matcher.py` decomposed.** The monolithic matcher module was split
  into focused submodules (scoring, candidate generation, tie-break) with
  behaviour-preserving tests.
- **#109 — `scratch/` and `demo/` out of `apps/`.** Non-shipping helpers moved
  out of the application tree so the packaged distribution is lean.

### Hygiene
- **#123 — `pyproject.toml` dependency cleanup.** Unused deps removed; extras
  re-organised; metadata tightened.
- **#124 — `.env.sample`.** Canonical env var reference now lives at the repo
  root with inline descriptions.
- **#107 / #118 — `.mailmap` finalised.** Single canonical identity; git
  shortlog and blame are now coherent.
- **#119 — `MILESTONES.md`.** Release ledger mapping tags → phases → commit
  ranges → key PRs.
- **#124 — LIC-1 closed.** License isolation (GPL `mutagen` opt-in extra) is
  documented end-to-end; diagnose HIGH finding cleared.
- **#125 — 6 MEDIUM+LOW diagnose cleanups.** Frozen datetime helpers, doc
  cross-links, and assorted lint-level findings from the diagnose audit.

### Documentation
- **#106 — Health audit.** Repo health report covering build, test, lint,
  coverage, and dependency posture.
- **#105 — Forensics audit.** Pre-release forensic pass on git history,
  licensing, and residual risks.
- **#114 — Progress report.** Post-v1.0 trajectory summary.
- **#121 — Diagnose audit.** Systematic audit producing HIGH/MEDIUM/LOW
  findings, all remediated in this patch (#124, #125).
- **#126 — `CODEBASE-MAP-v1`.** Full module tree: entry points, data flow,
  API surface, test mirror.

---

## Upgrade notes

No action required. `pip install -U music-dj-tools==1.0.1` or bump the pinned
version in your environment. Plugins and CLI entry points are unchanged.

If you maintain custom wrappers over `apply_plan` or `DjayPlaylistWriter`:
confirm you pass a destination path distinct from the source library — the new
self-guards will raise at call time if source == destination. This was always
the documented contract; v1.0.1 now enforces it.

---

## Metrics

| Metric | v1.0 | v1.0.1 | Δ |
|---|---|---|---|
| Tests passing | 2251 | 2327 | **+76** |
| Tests skipped | 45 | 45 | — |
| Tests failing | 0 | 0 | — |
| Diagnose HIGH findings | 3 | 0 | **−3** |
| Diagnose MEDIUM+LOW | 6 | 0 | **−6** |
| PRs merged since last tag | — | ~30 | — |

---

## Full PR ledger

Safety: #110, #111, #116.
Robustness: #113, #115, #124.
Refactor: #109, #120.
Hygiene: #107, #118, #119, #123, #125.
Docs: #105, #106, #114, #121, #126.
Merge-watch / release: #127.

See [`CHANGELOG.md`](CHANGELOG.md) for the Keep-a-Changelog summary and
[`.planning/milestones/`](.planning/milestones/) for per-phase detail.
