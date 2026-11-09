# beat-sync-sim: the shipped phase lock, closed loop, on real grids

The instrument behind PR #8872 (the out-of-reach re-join). The offline transition
renderer plays one constant ratio and has no phase lock, so it cannot show a Beat
Sync lock defect or fix. These scripts drive the shipped `phase-lock.ts` and
`beat-sync-math.ts` exactly the way both engines' 30 Hz ticks do, on the real
rekordbox grids, and report what the lock did: re-joins, seconds jumped back,
phase error p50 and p95, seconds spent with a flam of 10 ms or more, seconds of
incoming advanced. `render.py` then plays the incoming along the simulated path so
a re-join is heard as a jump.

Record of the first use: `(private path)`
(round 24 in `(private path)`).

| File | Purpose |
|---|---|
| `dump_grids.py` | Dump PQTZ grids from rekordbox ANLZ folders to `beats.json` (`{"out": [{n, bpm, t}], "in": [...]}`); `out` is the master |
| `sim.mjs` | One closed-loop run of the working tree's lock on a pair; prints one JSON line of statistics |
| `evalpairs.mjs` | Before/after over every incoming in `beats.json`, Master Tempo off and on; writes rows with trajectories |
| `bench.mjs` | Cost of one `phaseLockDecision`, in reach and on the out-of-reach re-join probe path |
| `render.py` | Clips, phase-error plot, table image and `compare.html` from the `evalpairs.mjs` rows (needs `ffmpeg`) |
| `compare.tmpl.html` | The listening page template (click-to-seek waveform per clip, space pauses) |

## Re-run

All `.mjs` scripts take the frontend directory first and load the TypeScript
through the unit tests' bundler, so they need `pnpm install` in
`apps/webui/frontend` and nothing else.

```bash
FE=apps/webui/frontend
# 1. grids: name=<ANLZ subfolder> pairs; the default root is rekordbox's own share folder in your home
uv run tools/beat-sync-sim/dump_grids.py /tmp/bs/beats.json out=P016/0000A1B2 in=P02D/0000C3D4
# 2. one run: master start s, follower start s, blend s, pitch range %, Master Tempo (1 on, 0 off)
node tools/beat-sync-sim/sim.mjs $FE /tmp/bs/beats.json 215.377 11.6 81.9 16 0
# 3. before/after: copy the pre-change lock beside the working tree's, then evaluate every incoming
git show origin/main:$FE/src/lib/rb/phase-lock.ts > $FE/src/lib/rb/zz-phase-lock-before.ts
node tools/beat-sync-sim/evalpairs.mjs $FE /tmp/bs/beats.json /tmp/bs/eval.json
rm $FE/src/lib/rb/zz-phase-lock-before.ts
# 4. cost per decision
node tools/beat-sync-sim/bench.mjs $FE /tmp/bs/beats.json
# 5. for the ear (audio paths and the master start come from a local config, see render.py's docstring)
uv run (private path) /tmp/bs/eval.json /tmp/bs/config.json /tmp/bs/out
```

`evalpairs.mjs` hard-codes the first study's frame (master from its bar 85 for 32
bars at 93.75 BPM, 16 percent range, each incoming joined at a start bar from
`START_BARS`); edit those constants for another pair, they are the experiment's
fixture, not the tool's.

## Reading the numbers

- `reseeks` and `backSec` (or `seekJumpSec`) are the loop: a re-join that lands
  behind the current position is the intro replaying.
- `p95Ms` stays high while the incoming's grid is outside the pitch range; no
  in-range tempo can hold that phase. Judge the fix by `backSec` and
  `progressSec`, and by p95 once the grid is back in reach.
- `flamSec` is the time the two kicks are 10 ms or more apart, the audible flam.
- Before and after run the same grids through two copies of the lock, so the
  comparison is deterministic: there is no noise band to allow for.

## Limits

- `dump_grids.py` writes the raw PQTZ times. The app shifts an MP3's grid by its encoder
  lead-in (`on_our_timeline`, the `/anlz` endpoint) and drops beats before zero, so compare
  against app positions only after the same shift. The lock statistics do not depend on it:
  a per-track constant offset moves both the grid and the start you pass by the same amount.
- The sim drives the lock functions, not the audio graph: it is not a substitute
  for hearing the app, only for reasoning about what the lock decides.
- `render.py` mixes both decks at equal gain with no EQ or fader moves on purpose:
  the lock is under test, not the plan's shape.
- The clips carry copyrighted audio and are never committed; publish them as a
  listening artifact and link it from the record.

Smoke test: `apps/webui/frontend/tests/unit/beat-sync-sim-tool.test.mjs` runs
`sim.mjs` on two identical constant grids and expects 0 re-seeks.
