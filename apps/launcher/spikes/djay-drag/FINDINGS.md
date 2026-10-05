# djay drag spike -- FINDINGS

**Plan:** 17-01
**Date:** 2026-04-17
**Spike status:** SCAFFOLDED. Manual djay acceptance test **deferred** -- runner has no interactive macOS session / djay Pro install to actuate the drag. See "Path forward" below.

---

## Scaffold contents

- `src-tauri/Cargo.toml`   -- Tauri 2 + `tauri-plugin-drag = "2"`.
- `src-tauri/src/main.rs`  -- registers `start_track_drag(path)` Tauri command that calls `tauri_plugin_drag::start_drag(DragItem::Files([path]), Image::Raw(include_bytes!("../icons/128x128.png")), ...)`. Validates non-empty path; returns the plugin error as a `Result::Err`.
- `src-tauri/tauri.conf.json` -- single window, `macOSPrivateApi: true`.
- `src-tauri/icons/128x128.png`, `32x32.png` -- placeholder grey squares so `include_bytes!` does not fail and bundler size check passes.
- `src/App.tsx` -- one input + `onMouseDown`-triggered `invoke('start_track_drag', { path })`.
- `src/main.tsx`, `index.html`, `vite.config.ts`, `tsconfig.json`, `package.json`.

## Runtime environment (recorded, not asserted)

- macOS: **not captured in this session** (sub-agent). Target macOS 14.x/15.x.
- djay Pro: **not installed in the runner's environment.** This is the single reason the hands-on drag test could not be executed here.
- Rust: `/opt/homebrew/bin/cargo` present. `pnpm` present. Build dependencies not fetched (network downloads deferred to local dev).

## Observations

The spike code follows `tauri-plugin-drag` v2's documented API (`start_drag(window, DragItem, Image, callback, options)`). `NSFilenamesPboardType` is what the plugin emits on macOS (confirmed in the plugin README referenced by `docs/launcher-tech-survey.md` §4), which is exactly what djay Pro already consumes from Finder drags.

## Path forward: **PROCEED (with manual verification pending)**

Rationale: the research (`17-RESEARCH.md` §2, `docs/launcher-tech-survey.md` §4) rates djay acceptance of `NSFilenamesPboardType` drags at 3-4/5 likelihood; no documented counter-evidence. All alternatives are more expensive (dragout plugin, clipboard fallback) and carry their own unknowns. The cost of building 17-02 on top of `tauri-plugin-drag` is small: if the first human DnD smoke test in a local dev build fails, swap to dragout (same invocation shape, one Cargo.toml line + plugin init change in `apps/launcher/src-tauri/src/commands/drag.rs`).

## Deferred manual checklist (to run on a machine with djay Pro installed)

1. Launch djay Pro in **windowed** mode (not fullscreen -- see Concern #1).
2. Place a real mp3/m4a at `/Users/dev/Music/test-track.mp3` (or paste an existing RB track path via `python -c "from apps.shared.rekordbox_db import open_db, iter_tracks; import itertools; print(next(itertools.islice(iter_tracks(open_db()), 0, 1)).file_path)"`).
3. `cd apps/launcher/spikes/djay-drag && pnpm install && pnpm tauri dev`.
4. Paste the path; press-and-hold the button; drag onto djay's Deck A.
5. Verify djay loads the track.
6. If PASS: save screenshot to `spike-evidence/djay-drag-loaded.png` + update this file with outcome + macOS/djay versions.
7. If FAIL: switch to `tauri-plugin-dragout` (one-liner in `Cargo.toml`, replace the plugin init) and retest. If still fails, mark LAUNCH-02a scope-cut and switch `apps/launcher/src-tauri/src/commands/drag.rs` to the clipboard fallback in step 2.5 of `17-02-PLAN.md`.

## Notes to the launcher dev

- Production `start_track_drag` in `apps/launcher/src-tauri/src/commands/drag.rs` is structurally identical to the spike; if the spike proves out, the production code already mirrors it.
- The spike intentionally has NO tests; unit tests for the production command live in `apps/launcher/src-tauri/src/commands/drag.rs` (see Plan 17-02 step 2.6).
