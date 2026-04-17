# Install notes

This project targets macOS + Python 3.14 in a local `.venv`. Most
dependencies come from `requirements.txt`, but a few pieces are **external
CLIs** that must be installed separately. This page collects them in one
place.

## Chromaprint (`fpcalc`) -- Phase 7 dedup

Phase 7 (dedup + tag unification) depends on Chromaprint's `fpcalc` CLI to
compute acoustic fingerprints. The pyacoustid Python package ships bindings
but shells out to `fpcalc` for the actual hashing.

Install on macOS:

```
brew install chromaprint
```

Verify:

```
scripts/check-chromaprint.sh
```

The script prints the installed version on success, or a helpful remediation
message and exits non-zero on failure. It is safe to run in CI and is the
recommended first step before `python -m apps.dedup.scan`.

Linux alternatives:

- Debian / Ubuntu: `sudo apt install libchromaprint-tools`
- Arch: `sudo pacman -S chromaprint`

When `fpcalc` is missing, `apps.shared.fingerprints.compute()` raises
`ChromaprintMissing`. The dedup CLIs degrade gracefully: they surface the
error with the remediation message instead of crashing the whole scan.

## Rekordbox database key (optional)

The Phase 1 fixture builder decrypts a snapshot of `master.db` once. If you
need to regenerate the fixture, follow `docs/rekordbox-key-setup.md` (if
present) or see pyrekordbox's docs; the key is cached under
`~/.pyrekordbox/` after the first decrypt.

## djay database

djay Pro AI's database lives at:

    ~/Music/djay/djay Media Library.djayMediaLibrary/MediaLibrary.db

No key needed. The schema is plain SQLite; see `apps/shared/djay_db.py`.
