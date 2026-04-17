# music-dj-tools v1.0-rc2

*Release candidate, 2026-04-17.*

Follow-up release candidate on top of [`v1.0-rc1`](RELEASE-NOTES-v1.0-rc1.md). Scope is focused: fix the wheel / sdist build so the GitHub release can ship signed assets, land the Phase 15 + 16 CLI surface and docs site, finish the Phase 16 Serato GEOB cue write, and close out Wave 5 through Wave 7 follow-up work plus the Wave 8 release-asset fixes.

See the [`v1.0-rc1` notes](RELEASE-NOTES-v1.0-rc1.md) for the full feature catalogue; this document covers what changed between rc1 and rc2 only.

## Why rc2 exists

The Wave 8 release-asset agent found that `v1.0-rc1` could not produce a wheel or sdist: `pyproject.toml` shipped without a `[build-system]` table and without package discovery config, so `python -m build` failed with `Multiple top-level packages discovered in a flat-layout: ['data', 'apps']`. rc2 fixes the build config so the GitHub release can attach real artefacts. We did not retag rc1; we cut a fresh rc2.

## What changed between rc1 and rc2

### Build + packaging
- `pyproject.toml` now declares the setuptools build backend (`[build-system]` with `setuptools>=77` + `wheel`) and package discovery (`[tool.setuptools.packages.find]` with `include = ["apps*"]` and exclusions for `data/`, `tests/`, `scripts/`, `docs/`, `open-dj/`, `.planning/`, `usb-profiles/`, and `htmlcov/`).
- License migrated to the SPDX form (`license = "Apache-2.0"` + `license-files = ["LICENSE"]`); the redundant `License :: OSI Approved :: Apache Software License` classifier was dropped. Both setuptools deprecation warnings flagged by the release-asset agent are gone.
- `.gitignore` now ignores `/dist/` and `/build/` so local build runs stay out of git.
- `python -m build --wheel --sdist` produces `music_dj_tools-1.0.0rc2-py3-none-any.whl` (247 files under `apps/`) and `music_dj_tools-1.0.0rc2.tar.gz`. No `data/`, `tests/`, or other repo-only directories leak into the wheel.

### Feature work closed since rc1

**open-dj CLI + docs site (Phase 15, Phase 16)**
- `open-dj-tool` CLI gained `export`, `import`, and `conformance` subcommands.
- MkDocs site scaffold landed with a `docs.yml` GitHub Actions workflow for Pages publishing.
- MkDocs strict link validation was relaxed for cross-repo references so the docs build stays green.
- CI Pages deploy step is best-effort until repository Pages is enabled, so the workflow no longer fails the pipeline.
- 12 additional conformance fixtures (Appendix B, corpus items 04 through 15).

**Serato adapter (Phase 16)**
- GEOB cue write plumbed through `SeratoAdapter.write()` via mutagen (P0 for Serato round-trip parity).
- Removed the cues-drop mask from the `03-8-hot-cues` conformance fixture now that GEOB write works.

**USB / Pioneer sync**
- Promoted the ad-hoc diff script to a typed `differ` module with a public API.
- Added a `diff-matrix` CLI and a parametrized pytest harness that runs the differ across N fixtures.
- USB diff-matrix tests now skip cleanly when the `rbox` package is unavailable.

**Phase 10.1 / CAT-06 closeout**
- Plan 01 rewritten as a delivery plan now that the spike has landed.
- VERIFICATION, UAT, and VALIDATION docs landed for CAT-06.
- Diff-matrix results published across all available USB export fixtures.

**Phase 4 hardening**
- `apply_ratings --live` now routes to the LIVE database paths instead of the staging copy (resolves [#1](https://github.com/former-work-account/music-dj-tools/issues/1)).

### Governance + docs + cleanup
- Governance audit + enhancement of `SECURITY.md`, Code of Conduct, `CONTRIBUTING.md`, and `NOTICE`.
- Anti-slop audit: 7 targeted fixes plus a systemic sweep across generated docs.
- Forensics pass 2 on project health after Wave 5.
- Re-audit of Wave 5 fix-code-review claims.
- Captured the SQLite WAL sidecar bug surfaced by the USB writer round-trip in `solutions/`.
- Stash audit + prune post Wave 6.
- Swept pending todos post Wave 5 fanout.

## What is still deferred

Same ledger as rc1: the long-standing deferrals listed in the [rc1 notes](RELEASE-NOTES-v1.0-rc1.md#what-is-not-in-v1) still apply (LAUNCH-03, SMART-04, live Litestream / R2 round-trip, Phase 11 SSE / WebSocket event bus, etc.). rc2 does not retire any of those.

## Install

Python 3.11 or newer. Same instructions as rc1:

```bash
git clone https://github.com/former-work-account/music-dj-tools.git
cd music-dj-tools
git checkout v1.0-rc2
python3 -m venv .venv
source .venv/bin/activate
pip install --no-build-isolation -r requirements.txt
python -m pyrekordbox download-key
make test
```

Or install the published wheel once the GitHub release is attached:

```bash
pip install music_dj_tools-1.0.0rc2-py3-none-any.whl
```

## License

Apache 2.0 (code); CC BY 4.0 (open-dj spec). See [`LICENSE`](LICENSE), [`NOTICE`](NOTICE), and [`open-dj/LICENSE-SPEC.md`](open-dj/LICENSE-SPEC.md).
