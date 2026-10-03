/**
 * Gig baseline track choice for mode_ratio_browser.mjs. Beat Sync aborts
 * when a follower's BPM is outside [0.84, 1.16] of deck 1, so the first
 * four playable rows are the wrong set on a mixed-tempo library.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  selectGigBaselineIds,
} from "../../scripts/perf/gig-baseline-tracks.mjs";

const beat = (stable_id, bpm) => ({ stable_id, bpm, file_exists: true });

test("skips leading playable tracks outside the Beat Sync tempo window", () => {
  const ids = selectGigBaselineIds([
    beat("slow", 90),
    beat("fast", 160),
    beat("a", 128),
    beat("b", 120),
    beat("c", 126),
    beat("d", 132),
  ]);
  assert.deepEqual(ids, ["a", "b", "c", "d"]);
});

test("deck 1 is the reference: a later cluster is not pulled in by an earlier outlier", () => {
  const ids = selectGigBaselineIds([
    beat("outlier", 100),
    beat("a", 140),
    beat("b", 146),
    beat("c", 150),
    beat("d", 138),
  ]);
  assert.deepEqual(ids, ["a", "b", "c", "d"]);
});

test("ratio bounds are inclusive", () => {
  const ids = selectGigBaselineIds([
    beat("master", 100),
    beat("lo", 84),
    beat("hi", 116),
    beat("mid", 100),
    beat("under", 83.9),
    beat("over", 116.1),
  ]);
  assert.deepEqual(ids, ["master", "lo", "hi", "mid"]);
});

test("an explicit empty beat grid is not a candidate even with a BPM", () => {
  const ids = selectGigBaselineIds([
    { stable_id: "no-grid", bpm: 128, beatgrid: { beats: [], beat_count: 0 } },
    beat("a", 128),
    beat("b", 128),
    beat("c", 128),
    beat("d", 128),
  ]);
  assert.deepEqual(ids, ["a", "b", "c", "d"]);
});

test("has_beatgrid false and a missing BPM are not grids", () => {
  assert.throws(
    () =>
      selectGigBaselineIds([
        { stable_id: "flagged", bpm: 128, has_beatgrid: false },
        { stable_id: "no-bpm", bpm: null },
        beat("only", 128),
      ]),
    /got 1/
  );
});

test("fewer than four compatible tracks fails naming the count", () => {
  assert.throws(
    () =>
      selectGigBaselineIds([
        beat("a", 100),
        beat("b", 104),
        beat("far", 180),
        beat("also-far", 70),
      ]),
    (error) => {
      assert.match(error.message, /got 2/);
      assert.match(error.message, /\[0\.84, 1\.16\]/);
      assert.match(error.message, /beat grids/);
      return true;
    }
  );
});

test("no grid-capable tracks names a count of zero", () => {
  assert.throws(
    () => selectGigBaselineIds([{ stable_id: "x", bpm: null, has_beatgrid: false }]),
    /got 0/
  );
});
