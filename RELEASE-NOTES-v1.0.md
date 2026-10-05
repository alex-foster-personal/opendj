# music-dj-tools v1.0

*Stable release, 2026-04-17.*

This is the **v1.0 stable** tag. It is the rc3 line plus one final wave of
adversarial-review remediation: 39 commits between [`v1.0-rc3`](RELEASE-NOTES-v1.0-rc3.md) and `v1.0`,
of which 25 are bug fixes and 17 came directly out of the codex / GPT-5.4
adversarial sweep across phases 02–18.

For the full feature catalogue, see the rc-series notes:

- [`v1.0-rc1`](RELEASE-NOTES-v1.0-rc1.md) — phases 1–18 feature ship + open-dj v0.2 spec.
- [`v1.0-rc2`](RELEASE-NOTES-v1.0-rc2.md) — wheel/sdist build, Phase 16 GEOB cue write, docs site.
- [`v1.0-rc3`](RELEASE-NOTES-v1.0-rc3.md) — runtime-deps in `pyproject.toml`, resource-leak +
  TOCTOU + live-write fixes, governance/audit pack feeding v1.1.

This document covers what changed between rc3 and v1.0 and the final v1.0 ship state.

## Ship state

- **Tests:** 2251 passing, 45 skipped (live-DB / integration / Rekordbox-availability
  gated), 0 failing on `make test`.
- **Requirements:** 53 v1 requirements catalogued in [`reqs.json`](reqs.json) — **51 shipped**,
  2 deferred to v1.1 (`LAUNCH-03`, `SMART-04`).
- **Phases:** 1–18 all delivered. Wave history and per-phase verification artifacts
  live under `.planning/`.
- **Wheel + sdist:** `music_dj_tools-1.0.0-py3-none-any.whl` and
  `music_dj_tools-1.0.0.tar.gz` both install cleanly into a fresh venv with
  runtime deps resolved automatically.

## What changed between rc3 and v1.0

### Codex / GPT-5.4 adversarial sweep (phases 02–18)

A 17-file codex-pro adversarial review was run across phases 02–18 after rc3 cut
(triage in PR #78). Each finding was verified, triaged, and remediated as an
isolated PR:

- `fix(2): score all RB candidates in matcher, not just first` (#74).
- `fix(3): playlist apply verify-in-tx and pgrep-fail-safe` (#81, P03-01/02).
- `fix(5): state writer rolls back if bus publish fails` (#75).
- `fix(9): safe default for spotify --max-tracks` (#77).
- `fix(12): apps.sets stop finalizes recorder and manifest` (#76).
- `fix(15): route open-dj-tool export through v0.2 wire serializer` (#79).
- `fix(adapters/serato): preserve subcrate playlist membership on read` (#65, R4).
- `fix(adapters/serato/geob): preserve cues and loops when __raw__ tag present` (#62, R4).
- `fix(adapters/serato): sanitize playlist name before writing subcrate file` (#67, R4).
- `fix(voice/bus): commit events and isolate fanout from producer` (#63, R4).
- `fix(pairings/add): close conn on any exception, not just PairingsError` (#64, R4).
- `fix(sync/usb/verify): serialize HashCache writes after pool joins` (#66, R4).
- `fix(webui/sqlite_backend): serialise update_track and trust fallback state` (#68, R4).
- `fix(cloud/lock): FakeS3Client treats If-Match '*' as S3 wildcard` (#69, R4).

### Drag-core fan-out v2 remediation (Tauri launcher)

Five contained fixes flagged by the v2 fan-out adversarial review of the launcher
drag-core path:

- `fix(launcher/drag-core): re-check mtime before sidecar rename` (#56, 2/3).
- `fix(launcher/drag-core): propagate sidecar fsync errors` (#55, 2/3).
- `fix(launcher/drag-core): percent-encode file URL in rekordbox Location` (#57, 2/3).
- `fix(launcher/drag-core): reject relative sidecar dirs in default_set` (#53, 2/3).
- `fix(state/ingest): derive stable_id from folder_path for streaming tracks` (#54).
- `fix(state/ingest): preserve BPM=0 as 0.0 instead of None` (#52, 3/3).

### Resource leaks rolled forward from rc3

The two leak fixes that were still open at rc3 cut time landed cleanly:

- `fix(hashing): close conn on _ensure_schema failure in HashCache` (#23).
- `fix(capture): close stderr_log fd on Popen failure` (#25).

### Security + licensing

- `fix: security red-team HIGH + 2 MEDIUM findings` (#59) addresses three issues
  surfaced by the rc3 adversarial security audit.
- `fix(license): move mutagen to optional [tags] extra (GPL vs Apache)` (#60)
  isolates GPL-licensed `mutagen` to an opt-in extra so the default install
  remains pure-Apache.

### Governance, audits, planning hygiene

- `chore(planning): redact secrets and identifiers from planning docs` (#92).
- `audit(M7): round-2 re-verification — REVIEW fixes held` (#40, 0 regressions,
  2 pre-existing blockers surfaced and tracked for v1.1).
- `audit(m2-m4): post-merge re-audit of milestones 2, 3, 4` (#47).
- `docs(audit): re-audit M5 and M6` (#49).
- `docs(audit): check-plan round 2 sweep` (#73, 18 phases, 19 open PRs at sweep time).
- `docs(audit): validate-work round 2 — Nyquist coverage sweep` (#72).
- `docs(adversarial): fan-out v2 triage (3/3 + 2/3 consensus)` (#51).
- `chore(merge-watch): round 2 report — 14 PRs merged, 6 conflicts flagged` (#80).
- `test(e2e): post-merge re-run of reconcile/sync-cues/open-dj-roundtrip` (#46).
- `chore(codex-triage): triage 17 GPT-5.4 codex-pro review files` (#78).
- `docs: GPT-5.4 Mux routing verification` (#70).
- `docs: Sourcery web-scanner integration research + pipeline design` (#61).
- `chore(codex): document ChatGPT Pro OAuth auth setup for codex CLI` (#58).

### Build + packaging

- Version bumped to `1.0.0` (rc3 suffix dropped).
- `reqs.json` regenerated against `.planning/REQUIREMENTS.md` (57 catalogued
  requirements, 53 in v1 scope).

## What is still deferred to v1.1+

Same ledger as rc3 plus the codex-sweep findings already filed but not P0 for v1:

- `LAUNCH-03` and `SMART-04` (the two pending v1 requirements).
- Live Litestream / R2 round-trip exercise.
- Phase 11 SSE / WebSocket event bus surface.
- M7 round-2 surfaced two pre-existing blockers (tracked in `.planning/`).
- Out of scope by design: in-app DSP/mixing engine, DRM-protected streaming
  playback, non-macOS targets in v1, RE beyond documented read/write needs,
  and becoming a controller hardware vendor.

The v1.1 milestone plan is in [`.planning/milestones/M8/`](.planning/milestones/M8/)
and incorporates the audit / consensus / type-hint / a11y / perf / security /
deps packs produced during rc3.

## Install

Python 3.11 or newer.

```bash
pip install music_dj_tools-1.0.0-py3-none-any.whl
```

Optional extras (Spotify, analysis, voice, cloud, ai, mutagen tags) are opt-in:

```bash
pip install 'music_dj_tools-1.0.0-py3-none-any.whl[all]'
```

Or from source:

```bash
git clone https://github.com/former-work-account/music-dj-tools.git
cd music-dj-tools
git checkout v1.0
python3 -m venv .venv && source .venv/bin/activate
pip install --no-build-isolation -r requirements.txt
python -m pyrekordbox download-key
make test
```

## Credits

v1.0 is the result of an 18-phase compound-engineering build executed by an
agent swarm (Mux + codex-pro + Claude Sonnet 4.5/5 + GPT-5.4) coordinated by
the project maintainer, with adversarial review, triage, remediation, and
verification gated by `verify-work` / `check-plan` / `validate-work` cycles.
Final adversarial sweep used a best-of-n consensus across 17 phases × 3 models
(see PR #34, rc3).

## License

Apache 2.0 (code); CC BY 4.0 (open-dj spec). See [`LICENSE`](LICENSE),
[`NOTICE`](NOTICE), and [`open-dj/LICENSE-SPEC.md`](open-dj/LICENSE-SPEC.md).
