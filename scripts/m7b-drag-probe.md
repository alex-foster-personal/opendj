# Phase 18 Drag-Drop Probe SOP

**Purpose:** Empirically verify which DJ-app drop targets accept a
`tauri-plugin-drag` Finder-style drag on the user's current machine. Fills in
`docs/m7b-drag-matrix.md`.

**Why manual:** Cross-app drag on macOS is not scriptable from CI. A 10-minute
manual pass once per macOS/DJ-app upgrade beats 40 lines of brittle
Accessibility-API scripting.

## Preconditions

1. Hyper-K Launcher dev build (Phase 17) runs and shows the palette.
2. One fixture track exists at `tests/fixtures/drag_probe/probe.mp3` (any local
   audio file, re-used for each vendor).
3. Rekordbox / Serato DJ Pro / Traktor Pro are installed. djay Pro is already
   covered by Phase 17.

## Steps (one pass per vendor x drop-target x mode combination)

For each row in `docs/m7b-drag-matrix.md`:

1. Quit all DJ apps (`pgrep -if "(rekordbox|serato|traktor|djay)"` returns
   nothing). Open ONLY the vendor being probed.
2. Choose the mode column: windowed or full-screen. Resize / full-screen the
   DJ app accordingly.
3. Launch the Hyper-K dev build with `?probe=1` in the URL fragment:
   `open "hyperk://probe=1"` (or click the "Probe" menu in the tray when this
   flag is on).
4. The probe pane shows a 3x4 grid of buttons, one per vendor x drop-target.
   Click the button matching this row.
5. Observe: does the track appear in the DJ app's target surface?
   - Rekordbox collection: new track row in Collection panel.
   - Rekordbox deck: deck waveform populates.
   - Serato Files panel: track shows under Files list.
   - Serato deck: deck waveform populates.
   - Traktor browser: track appears in highlighted playlist / unsorted.
   - Traktor deck: deck stripe populates.
6. Record `pass`, `fail`, or `partial` in the matrix. Add a short note on
   partial (e.g., "library-add works, deck-load does not").
7. If `fail`: click "Copy path" in the launcher toast and paste into the DJ
   app's own File > Import dialog to confirm the file is valid (isolates
   drag-vs-file issue).
8. Quit the vendor. Move to the next row.

## Probe output

`docs/m7b-drag-matrix.md` filled in. Any row marked `fail` becomes an entry
in the "Concerns" section of `18-VERIFICATION.md` and may trigger a Phase
18.1 fallback adapter plan.

## Reproducibility

- macOS version (from `sw_vers -productVersion`) recorded in every row.
- DJ app version (from Menu > About) recorded in every row.
- Re-run the full matrix when any of these change.
