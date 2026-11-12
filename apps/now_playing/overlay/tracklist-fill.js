// Overlay list fallback (NOWP, issue #10341). The overlay page (#10339) inlines this
// file and calls fillPrevious when the resolver knows fewer than n previous tracks.
//
// previous: resolver entries, newest first, each with stable_id and played_at.
// feedRows: rows of GET /api/v1/now-playing/tracklist, oldest first as served.
// current:  the playing entry (stable_id, played_at) or null.
// Result:   previous.slice(0, n), then feed rows newest first until length n.
// A feed row is skipped when its (stable_id, played_at) matches a previous entry or
// current, when it is not strictly older than the oldest previous entry, or (with no
// previous) when it is the newest feed row and carries current's stable_id, since the
// playing track is logged too. played_at is canonical UTC on both sides, so plain
// string comparison orders it. Feed rows keep their redacted ID labels as served.
// No DOM, no network: pure data in, data out.
function fillPrevious(previous, feedRows, n, current) {
  if (n <= 0) return [];
  const out = previous.slice(0, n);
  const keyOf = (e) => e.stable_id + "|" + e.played_at;
  const seen = new Set(out.map(keyOf));
  if (current) seen.add(keyOf(current));
  const oldest = out.length ? out[out.length - 1].played_at : null;
  const newestFirst = feedRows.slice().reverse();
  newestFirst.forEach((row, i) => {
    if (out.length >= n || seen.has(keyOf(row))) return;
    if (oldest !== null && !(row.played_at < oldest)) return;
    if (oldest === null && i === 0 && current && row.stable_id === current.stable_id) return;
    seen.add(keyOf(row));
    out.push(row);
  });
  return out;
}

globalThis.OpenDjTracklistFill = { fillPrevious };
