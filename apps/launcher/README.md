# Hyper-K Launcher

Tauri 2.x menu-bar palette for the music-dj-tools monorepo. Phase 17
delivers v1: global hotkey opens a `cmdk` palette, typing filters the
library via SQLite FTS5, dragging a row loads the track into djay Pro.

## Status (as of 2026-04-17)

- Plan 17-01: spike scaffolded at `spikes/djay-drag/`; manual djay drag test
  deferred. See `spikes/djay-drag/FINDINGS.md` (path forward = PROCEED).
- Plan 17-02: Tauri shell in `src-tauri/`, React palette in `src/` with the
  `cmdk` wrapper. Hotkey = `Alt+Space` (fallback `Ctrl+Cmd+Space`). Samples
  render; drag path = `tauri-plugin-drag` PROCEED branch.
- Plan 17-03: FTS5 search + frecency backend in Rust
  (`src-tauri/src/commands/{search,frecency}.rs`), two-tier cache in
  `src/hooks/{useSearch,useFrecency}.ts`, bootstrap script in
  `scripts/bootstrap_db.py`, latency harness in `scripts/latency_check.py`.

## Running locally

Requires macOS 14+, Xcode Command Line Tools, Rust (stable), Node 20+, pnpm.

```bash
cd apps/launcher
pnpm install
# First-run bootstrap (Phase 5 shared-state DB is the preferred source; we
# fall back to a one-off index built from Rekordbox + djay readers):
python scripts/bootstrap_db.py
pnpm tauri dev
```

Then press `Alt+Space` to toggle the palette. Type >=2 chars to filter.
Click-and-drag a row onto djay Pro's Deck A / B to load.

## Tests

```bash
pnpm test            # vitest (React component + hook tests)
pnpm test:rust       # cargo test (pure-Rust helpers: search, frecency, drag)
```

## Verified scenarios

- Unit: Rust `search_tracks_impl` returns expected hits (3 tracks in-memory fixture).
- Unit: `build_fts_query` sanitises quotes and appends `*` prefix match.
- Unit: frecency `decay` is 1.0 at now, ~0.3679 at 10 days (10-day half-life).
- Unit: `record_drag_impl` increments `drags` + updates `last_dragged_at`.
- Unit: drag validator rejects empty and non-existent paths.
- React: `<TrackRow>` fires `start_track_drag` on both `onMouseDown` and `onSelect`.
- React: `useSearch` does local match-sorter for 1-3 chars, debounced FTS5 for >=4.
- Python (fixture): `tests/test_launcher_fts5.py` builds an in-memory `tracks_fts`
  and asserts a `mira*` query resolves to the seeded "Neon Orchard" row
  (`@pytest.mark.requirement("LAUNCH-01")`).
- Manual djay drag: **deferred** (see `spikes/djay-drag/FINDINGS.md`).
- Latency P95 < 50ms on 5K tracks: **deferred** until `pnpm install` + `cargo
  run --example search_bench` runnable in this workspace (network).

## Open questions

1. Real branded tray + drag icons -- v1 uses grey placeholders.
2. `Info.plist` LSUIElement injection -- see `src-tauri/Info.plist.fragment.md`.
   Tauri v2 does not expose this via `tauri.conf.json` yet; manual
   post-build step documented until `tauri-cli` supports it directly.
3. Frecency decay half-life: currently 10 days. Plausible knob; revisit once
   user has real usage data.
4. djay in fullscreen mode breaks cross-space DnD on some macOS versions;
   users must run djay in windowed mode (documented here).

## Gap / integration points

- **Phase 18 drag dispatcher:** lives in `apps/launcher/drag-core/` (its own
  crate) and is wired end-to-end through `TauriHostBridge` (Phase 17) and
  `Dispatcher::default_set()` (Phase 18). See
  `.planning/phases/17-*/17-02-SUMMARY.md` and
  `apps/launcher/src-tauri/src/commands/drag.rs` for the production wiring.
- **Phase 5 shared state DB:** launcher prefers `data/state/state.db` when
  present, else falls back to `data/launcher-bootstrap.sqlite`. Once Phase 5
  ships, the bootstrap script becomes a no-op (prints SHARED_STATE_READY).
- **Notarization / code-signing:** left blank in `tauri.conf.json`.
  Distribution pipeline is out-of-scope for Phase 17 (C9).
