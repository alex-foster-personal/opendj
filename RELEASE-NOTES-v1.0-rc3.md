# music-dj-tools v1.0-rc3

*Release candidate, 2026-04-17.*

Follow-up release candidate on top of [`v1.0-rc2`](RELEASE-NOTES-v1.0-rc2.md). Scope is narrow: ship the adversarial remediation wave that ran against rc2. That wave landed the missing runtime-dependency declaration in `pyproject.toml`, fixed a handful of resource-leak and TOCTOU bugs surfaced by the second-pass code review, hardened several live-write paths flagged by the red-team smoke test, and produced a large docs/audit pack (security, CI/CD, type hints, deps/supply chain, accessibility, performance, consensus) that will feed v1.1.

See the [`v1.0-rc1` notes](RELEASE-NOTES-v1.0-rc1.md) for the full feature catalogue and the [`v1.0-rc2` notes](RELEASE-NOTES-v1.0-rc2.md) for the build + packaging fixes that unlocked signed assets. This document covers what changed between rc2 and rc3 only.

## Why rc3 exists

rc2 shipped a working wheel and sdist, but a follow-up adversarial sweep surfaced three classes of issue that gate a clean v1.0 tag:

1. **Missing runtime dependencies.** `pyproject.toml` declared the build backend and package discovery but never declared `[project.dependencies]`, so `pip install music_dj_tools-1.0.0rc2-py3-none-any.whl` produced an importable package that failed at runtime on the first `import` of `pyrekordbox`, `mutagen`, `click`, etc. Users had to install `requirements.txt` by hand before the wheel would do anything. (V2 adversarial CRITICAL.)
2. **Resource-leak + TOCTOU bugs missed by the first code review.** Second-pass adversarial code review (17 prior reviews crosschecked) flagged real issues: two file-descriptor leaks on failure paths, one SQLite connection leak on schema-migration failure, and one TOCTOU race on the events-publisher close flag.
3. **Live-write and CLI-contract bugs surfaced by the rc2 red-team smoke test.** `apply_analysis --live` was writing to the staging DB path, Serato GEOB writes could corrupt an MP3 if the adapter errored mid-write, the dedup reversal script was generated *after* the live write, and `apply_cues --bulk` was rejecting the documented "no explicit `--tracks`" form.

rc3 addresses all three classes. We did not retag rc2; we cut a fresh rc3.

## What changed between rc2 and rc3

### Build + packaging
- `pyproject.toml` now declares `[project.dependencies]` (pyrekordbox, kaitaistruct, mutagen, rich, pyacoustid, psutil, PyYAML, jsonschema, rfc8785, fastapi, uvicorn, httpx, setuptools<81) plus `[project.optional-dependencies]` groups for `analysis`, `voice`, `cloud`, `spotify`, `ai`, and an `all` aggregate extra (PR #31). `pip install music-dj-tools` now resolves a working environment out of the box.
- Version bumped to `1.0.0rc3`.

### Adversarial-review fixes (code-review-v2, verify-work-v2)
- `fix(events): read _closed flag under lock in publish and close` (PR #22) closes a TOCTOU window where a concurrent `publish()` could fire after `close()` had already returned.
- `fix(db): close connection on pragma/migration failure in open_rw` (PR #21) stops leaking SQLite connections when migration or PRAGMA setup raises.
- `fix(sets): close stderr log file handle in stop_capture` (PR #11) closes the stderr log fd that was leaking on the Phase 12 recorder stop path.
- `fix(sets): validate path traversal unconditionally in list_segments` (PR #9) removes a branch where path-traversal validation could be skipped on certain inputs.
- `fix: check-plan V2 blockers (CAT-06 ledger + daemon indirection + coverage matrix)` (PR #32) clears the three blockers the post-merge check-plan re-run flagged.
- `fix(spotify): add encoding=utf-8 to cache write_text and read_text` (PR #41) fixes a cache-corruption path on non-UTF-8 locales.

### Red-team smoke-test fixes (rc2-smoke)
- `fix(4): apply_analysis --live routes to LIVE DB paths` (CRITICAL adversarial bug 1) now writes where `--live` promised.
- `fix(16): Serato GEOB write backs up MP3 before mutation` (adversarial bug 2) takes an on-disk backup prior to `mutagen` mutation so a mid-write crash no longer corrupts the file.
- `fix(7): dedup/apply writes reversal script before live write` (adversarial bug 3) re-orders the reversal-script emit ahead of the destructive step so operators always have a rollback.
- `fix(4): apply_cues --bulk no longer requires --tracks` (adversarial bug 4) restores the documented rc1 CLI contract.
- `fix(writer): move bus.publish inside _tx block for atomicity` aligns the writer event publish with the transaction boundary.

### Governance, audits, and docs (feeding v1.1)
- `docs(adversarial): second-pass code review v2` (PR #15) is the review document that produced the fixes above.
- `docs(triage): bot-review triage across 8 PRs` (PR #18) plus round 2 across 4 new PRs (PR #29) captures Sourcery / CodeRabbit dispositions.
- `audit(check-plan-v2): post-merge re-run of GSD check-todos + verify-phase` (PR #24).
- `docs(verify-work): v2 post-merge spot-check of all 17 phases` (PR #26, 0 regressions).
- `docs(adversarial): best-of-n consensus from 17 phases x 3 models` (PR #34).
- `docs(forensics): HEALTH-3 post-wave-8+ audit` (PR #27).
- `audit: test hygiene sweep` (PR #30).
- `docs(adversarial): CI/CD config audit 2026-04-17` (PR #35).
- `docs(security): adversarial security red team audit` (PR #36).
- `docs(adversarial): type-hint audit (mypy 1.20.1, 119 errors, 46 files)` (PR #38).
- `docs(audit): adversarial docs audit V2 for post-PR-5 drift` (PR #39).
- `docs(a11y): audit Tauri launcher UI against WCAG 2.1 AA` (PR #33).
- `audit: dependency + supply-chain audit` (PR #43).
- `docs(audit): re-validate phases 2-18 post-merge-sweep` (PR #42).
- `docs(perf): performance red team (35 findings across 10 probes)` (PR #44).
- `docs(diagnostics): 3 post-audit issue diagnostics` (PR #17).
- `docs(solutions): Critical Pattern #1 SQLite WAL sidecar rule` (PR #4) plus Sourcery nit cleanup (PR #19).
- `feat(launcher/ux): first-run notification + tray tooltip + drag toasts` (PR #16).
- `docs(M8): v1.1 milestone plan` (PR #14).

## What is still deferred

Same ledger as rc1 / rc2: the long-standing deferrals listed in the [rc1 notes](RELEASE-NOTES-v1.0-rc1.md#what-is-not-in-v1) still apply (LAUNCH-03, SMART-04, live Litestream / R2 round-trip, Phase 11 SSE / WebSocket event bus, etc.). rc3 does not retire any of those.

The capture / hashcache fd-leak and schema-migration-connection-leak PRs are still open at rc3 cut time (PRs #25, #23); both are contained single-file fixes on failure paths that do not block rc3 and will roll into the next tag.

## Install

Python 3.11 or newer. Same instructions as rc2:

```bash
git clone https://github.com/former-work-account/music-dj-tools.git
cd music-dj-tools
git checkout v1.0-rc3
python3 -m venv .venv
source .venv/bin/activate
pip install --no-build-isolation -r requirements.txt
python -m pyrekordbox download-key
make test
```

Or install the published wheel once the GitHub release is attached. Because rc3 declares runtime deps in `pyproject.toml`, the wheel now pulls its own dependencies:

```bash
pip install music_dj_tools-1.0.0rc3-py3-none-any.whl
```

Optional extras (Spotify, analysis, voice, cloud, ai) are opt-in:

```bash
pip install 'music_dj_tools-1.0.0rc3-py3-none-any.whl[all]'
```

## License

Apache 2.0 (code); CC BY 4.0 (open-dj spec). See [`LICENSE`](LICENSE), [`NOTICE`](NOTICE), and [`open-dj/LICENSE-SPEC.md`](open-dj/LICENSE-SPEC.md).
