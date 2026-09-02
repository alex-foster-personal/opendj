# apps/cloud -- music-dj-tools cloud sync (Phase 11)

## Read this first: three different things are called "cloud" or "sync"

They are separate components with separate jobs, and conflating them costs
real time. Wed 2 Sep 2026 an agent read the cloudsync sync protocol, found no
file surface, and reported "CloudSync cannot host files" -- while the file
surface sat in this package the whole time. Use this table before reading
anything else.

| You want to... | Component | Where |
| --- | --- | --- |
| Store or fetch a **file** (stem bundle, HQ audio, test fixture, any large artifact) | **R2 asset store**, content-addressed `assets/<sha256[:2]>/<sha256>`, immutable | `apps/cloud/asset_store.py`, ADR 06 `specs/design_decision_06.md` |
| Replicate **`state.db` itself** to R2 so another machine can restore it byte-identical | **Litestream** WAL streaming | `apps/cloud/litestream.yml`, `s3://music-dj-state/wal/` |
| Converge **library rows** (tracks, playlists, cues) between a hub and several spokes, with tombstones and conflict rules | **CloudSync protocol** (hub/spoke row sync) | `apps/sync_hub/service.py` (`/sync/hello`, `/sync/push`, `/sync/pull`), `apps/webui/server/routes/cloudsync.py` (policy/machine configuration), `specs/cloudsync-spec.md`, ADR 04 |
| Keep raw **audio folders** in step between two laptops peer-to-peer | **Syncthing** bootstrap | this README, section 2 below |
| Stop two machines replicating at once | **Single-writer lock** | `apps/cloud/lock.py`, `s3://music-dj-state/LOCK.json` |

The CloudSync **protocol** has no file surface by design: it moves rows. The
**asset store** has no row semantics by design: it moves bytes by hash. A
feature that needs both (a stem bundle plus the row that points at it) uses
both, which is what `track_locations` rows with `kind='remote'` are for.

Credentials for every R2 path come from Doppler `general/dev_personal`
(`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`); see
`apps/cloud/config.py`. Never from `.env`.


Litestream-based SQLite replication to Cloudflare R2, plus a cooperative
single-writer lock (D2) and a Syncthing bootstrap for audio files.

## What this does

1. **DB sync (CAT-04a):** Litestream streams the WAL of
   `data/state/state.db` to `s3://music-dj-state/wal/` continuously. On a
   second laptop, `litestream restore` rebuilds the DB byte-identical.
2. **Audio sync (CAT-04b):** Syncthing peer-to-peer over `~/Music/DJ
   Library/`. R2 audio bucket is wired in `apps/cloud/s3_audio.py` but
   NOT enabled by default.
3. **Single-writer lock (D2):** `apps/cloud/lock.py` stores a JSON lock
   object at `s3://music-dj-state/LOCK.json`. TTL 10 minutes. Two
   laptops cannot replicate at the same time.

## First-time setup

```bash
# 1. Install binaries.
brew install litestream syncthing

# 2. Doppler bootstrap (one-off; follow Doppler CLI docs).
doppler setup -p music-dj-tools -c prod

# 3. Create R2 buckets + lifecycle.
doppler run -p music-dj-tools -c prod -- bash scripts/r2-bootstrap.sh

# 4. Configure Syncthing folder share.
bash scripts/setup-syncthing.sh

# 5. Install the launchd user agent for Litestream.
#    IMPORTANT: edit the plist first to replace /Users/YOU with $HOME.
sed "s|/Users/YOU|${HOME}|g" \
    apps/cloud/launchd/com.musicdj.litestream.plist \
    > ~/Library/LaunchAgents/com.musicdj.litestream.plist
launchctl load ~/Library/LaunchAgents/com.musicdj.litestream.plist

# 6. Verify:
tail -f /tmp/music-dj-litestream.out
```

### `brew services` alternative

If you prefer `brew services` over launchd, create a local formula overlay
that wraps `apps.cloud.replicate` under Doppler. The tradeoff: brew
services runs as a LaunchDaemon (system scope), while our plist is a
LaunchAgent (user scope). Default to the plist.

## Restoring on a new laptop

```bash
# Pull the latest replica.
doppler run -p music-dj-tools -c prod -- \
    litestream restore -config apps/cloud/litestream.yml data/state/state.db

# Sanity-check.
sqlite3 data/state/state.db "PRAGMA integrity_check;"
```

This is destructive locally: if `data/state/state.db` exists, Litestream
refuses by default. Add `-if-replica-exists` or move the existing file
aside first. Phase 12+ will add a GUI flow with backup rails.

## I broke it

### "Another laptop has the lock"

`python -m apps.cloud.replicate` refuses to start; stderr shows the holder.

- **Wait:** the lock auto-expires after 10 minutes of inactivity.
- **Take over manually (dangerous):** stop Litestream on the other laptop
  first, then on this one:
  ```bash
  doppler run -- aws s3api delete-object \
      --endpoint-url "https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com" \
      --bucket music-dj-state --key LOCK.json
  ```
  This is a manual override. Only do it if you are 100% sure no one else
  is writing.

### "Litestream keeps crashing"

Check `/tmp/music-dj-litestream.err`. Common causes:
- R2 creds rotated -- rerun `doppler setup`.
- DB path missing -- Phase 5 shared-state DB has not been initialised yet
  (`python -m apps.shared.state.cli init`).
- Disk full on `/` for the WAL checkpoint staging.

### "How do I fall back to local-only?"

```bash
launchctl unload ~/Library/LaunchAgents/com.musicdj.litestream.plist
```

Kill Syncthing via `brew services stop syncthing`. The web UI continues
to work against the local DB.

## Cost model

- R2 storage: ~$0.015/GB/month. A full WAL history of our 1,741-track
  library is under 1 GB at any given time, so approximately **$0.015/month**.
- R2 egress: **zero**, which is the entire reason we chose R2 over S3/B2.
- Audio opt-in (`apps/cloud/s3_audio.py`): 16 GB at ~$0.015/GB/mo =
  **$0.24/month**. Enable only if you want cross-laptop cold-start.
- Syncthing: no cloud cost; peer-to-peer over Tailscale/LAN.

## Reused by M7 launcher (Phase 17)

The launcher (Phase 17) will invoke `python -m apps.cloud.replicate`
exactly as the launchd plist does, but from within the Tauri process. No
extra cloud surface -- Phase 17 is a thin GUI over the same daemon.

## Safety rails

- **Live-DB writes.** Our shared-state DB is NOT a Rekordbox or djay live
  DB; the "never auto-close the DJ app" rule does not apply to writes
  here. But Litestream is a long-running writer, so the cooperative lock
  enforces the single-writer property between laptops.
- **No secrets in the repo.** All R2 creds come from Doppler; the
  `.env*` suffix is gitignored at the repo root.
- **Lock TTL ceiling.** 10 minutes; never raise without an ADR.
- **Syncthing never runs as root.** `scripts/setup-syncthing.sh` exits
  if invoked under sudo.
- **`litestream restore` is destructive.** Document only; no CLI wrapper
  yet. Phase 12+ adds a backup rail.
