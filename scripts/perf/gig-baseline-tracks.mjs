/**
 * Choose the four Gig-baseline tracks for PERFMODE-15.
 *
 * Beat Sync is engaged for the baseline (a real synced gig, not four
 * independent decks). Phase lock requires every follower's BPM to sit
 * inside the pitch window of deck 1, and a beat grid to lock to. The
 * first four playable library rows are not that set.
 *
 * Inputs are listing rows from `GET /api/v1/tracks` (the same payload
 * `mode_ratio_browser.mjs` already pages). That payload carries `bpm`.
 * It does not carry the beat array; a positive finite `bpm` is the
 * listing's evidence the track has a tempo grid. An explicit `beatgrid`
 * / `beat_grid` / `has_beatgrid` on the row wins when present: an empty
 * or missing grid is skipped even if `bpm` is set.
 */

export const GIG_TEMPO_RATIO_MIN = 0.84;
export const GIG_TEMPO_RATIO_MAX = 1.16;
export const GIG_BASELINE_DECK_COUNT = 4;

function finitePositiveBpm(value) {
  const bpm = typeof value === "number" ? value : Number.NaN;
  return Number.isFinite(bpm) && bpm > 0 ? bpm : null;
}

/** Listing-row beat grid. Explicit grid fields override the bpm fallback. */
export function listingRowHasBeatGrid(row) {
  if (row == null || typeof row !== "object") return false;
  if (row.has_beatgrid === false) return false;
  const grid = row.beatgrid ?? row.beat_grid;
  if (grid != null && typeof grid === "object") {
    const beats = Array.isArray(grid.beats) ? grid.beats.length : null;
    const count = typeof grid.beat_count === "number" && Number.isFinite(grid.beat_count)
      ? grid.beat_count
      : beats;
    if (count == null || count < 2) return false;
    return finitePositiveBpm(row.bpm) != null;
  }
  if (row.has_beatgrid === true) return finitePositiveBpm(row.bpm) != null;
  return finitePositiveBpm(row.bpm) != null;
}

function eligibleRows(rows) {
  const eligible = [];
  if (!Array.isArray(rows)) return eligible;
  for (const row of rows) {
    if (row == null || typeof row.stable_id !== "string" || row.stable_id === "") continue;
    if (!listingRowHasBeatGrid(row)) continue;
    const bpm = finitePositiveBpm(row.bpm);
    if (bpm == null) continue;
    eligible.push({ stable_id: row.stable_id, bpm });
  }
  return eligible;
}

function withinTempoWindow(masterBpm, followerBpm) {
  const ratio = followerBpm / masterBpm;
  return ratio >= GIG_TEMPO_RATIO_MIN && ratio <= GIG_TEMPO_RATIO_MAX;
}

/**
 * Four stable ids, deck 1 first. Followers are within
 * [GIG_TEMPO_RATIO_MIN, GIG_TEMPO_RATIO_MAX] of deck 1's BPM.
 * Throws when no such four exist; the message names how many compatible
 * tracks the best deck-1 candidate actually had (including deck 1).
 */
export function selectGigBaselineIds(rows) {
  const eligible = eligibleRows(rows);
  let bestCount = eligible.length === 0 ? 0 : 1;
  for (let i = 0; i < eligible.length; i += 1) {
    const group = [eligible[i]];
    for (let j = i + 1; j < eligible.length && group.length < GIG_BASELINE_DECK_COUNT; j += 1) {
      if (withinTempoWindow(eligible[i].bpm, eligible[j].bpm)) group.push(eligible[j]);
    }
    if (group.length > bestCount) bestCount = group.length;
    if (group.length === GIG_BASELINE_DECK_COUNT) {
      return group.map((row) => row.stable_id);
    }
  }
  throw new Error(
    `need ${GIG_BASELINE_DECK_COUNT} tempo-compatible tracks with beat grids ` +
      `(follower BPM within [${GIG_TEMPO_RATIO_MIN}, ${GIG_TEMPO_RATIO_MAX}] of deck 1), ` +
      `got ${eligible.length === 0 ? 0 : bestCount}`
  );
}
