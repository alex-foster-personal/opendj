# Dedup workflow (Phase 7)

End-to-end flow for detecting duplicate tracks, picking a canonical file
per cluster, rewriting Rekordbox playlist links, and (later, in
`apps.tags.apply`) writing unified tags back into the audio container.

## Prerequisites

* `brew install chromaprint` -- ships the `fpcalc` CLI pyacoustid calls.
  Verify with `scripts/check-chromaprint.sh`.
* `pip install -r requirements.txt` -- pulls pyacoustid + psutil.

## Steps

### 1. Scan the library

```
python -m apps.dedup.scan
```

Walks `paths.MUSIC_ROOTS`, fingerprints each audio file via pyacoustid,
persists rows to `data/dedup/phase7.sqlite:fingerprints`. Incremental on
re-run (skip when size + mtime match the cache). Use `--force-recompute`
to refresh everything.

### 2. Find clusters

```
python -m apps.dedup.find_clusters
```

Single-linkage union-find over pairwise chromaprint similarity. Default
threshold is `0.92`. Writes:

* `data/dedup/clusters.csv` -- review-friendly list of cluster rows.
* `data/dedup/manual-review.csv` -- borderline clusters (duration delta
  > 3s or cluster size > `--max-cluster-size`).

Canonical selection follows the D2 tie-break ladder:
bitrate → size → duration → canonical root → oldest mtime.

Inserts rows into `duplicate_clusters` + `track_aliases` (same tables
Phase 5 shared-state will absorb later).

### 3. Dry-run apply

```
python -m apps.dedup.apply
```

Joins clusters/aliases against the live Rekordbox DB to find playlist
entries that still reference alias paths. Writes
`data/dedup/rewrite-plan.csv` + `rewrite-summary.md`. **Never** mutates
any DB or audio file.

### 4. Cautious live apply (2-3 clusters first)

```
python -m apps.dedup.apply --live --i-understand-the-risks \
    --clusters 1,2,3
```

Safety rails (all must pass):

1. Typed confirm `yes i understand` at stdin.
2. Refuse if Rekordbox is open (pgrep -if rekordbox).
3. Timestamped backup of `master.db` under
   `data/dedup/backups/master.YYYYMMDD-HHMMSS.db`.
4. Update `FolderPath` rows via pyrekordbox, commit.
5. Re-open the DB, verify every row now reads the canonical path.
6. Emit `reverse-dedup-YYYYMMDD-HHMMSS.sh` that restores the backup.

On verification failure the backup is copied back automatically and the
batch fails.

### 5. Tag unification (Plan 02)

Plan 02 (`apps.tags.*`) consumes the canonical paths and writes unified
tags into the audio containers (ID3v2.4 / MP4 atoms / Vorbis). See
[tag-unification.md](tag-unification.md).

## Safety invariants

* Never deletes or renames audio files.
* Never writes `master.db` without the 6 rails above.
* `--archive-duplicates` is NOT implemented in v1 (logged as
  `NotImplementedError`).
* Rating / djay writes are Plan 02's job; this plan only rewrites RB
  playlist links.

## Troubleshooting

* `ChromaprintMissing`: install fpcalc (`brew install chromaprint`).
* `RewriteRow.rb_content_ids` empty on dry-run: the alias path was
  normalised differently between RB and the filesystem. Check
  `FolderPath` case + trailing-slash conventions.
* All rewrites verified but `reverse-dedup-*.sh` not needed: still
  emitted so the batch is undoable. Safe to delete after a week.
