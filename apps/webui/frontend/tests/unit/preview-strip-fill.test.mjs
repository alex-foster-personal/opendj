import assert from "node:assert/strict";
import { before, test } from "node:test";

import { loadTypeScriptModule } from "./load-typescript.mjs";

// preview-strip-fill (NATIVE-21): rows in view fill their Preview strip
// without a click, in batches, with backoff for ids still being written.
//
// Regression lines:
// - if a visible strip-less row is not in the next batch then broken
// - if an id already in flight is asked again then broken
// - if a pending id is re-asked sooner than 2 s, 4 s, ... or never capped at 15 s then broken
// - if an id that scrolled away is still asked then broken
// - if a filled or not-pending id is asked again while in view then broken
// - if a reopened (refreshed) row is never asked again, or a pending one loses its backoff, then broken
//
// [if] visible rows are not filled in batches with backoff [then] fail, [else stop].

let m;
before(async () => {
  m = await loadTypeScriptModule("src/lib/rb/preview-strip-fill.ts");
});

/** Fake clock + timers + server; `answer(ids)` decides each batch. */
function harness(answer) {
  let now = 0;
  const timers = [];
  const calls = [];
  const strips = {};
  const filler = new m.PreviewStripFiller({
    fetchBatch: async (ids) => {
      calls.push({ at: now, ids: [...ids] });
      return answer(ids, calls.length);
    },
    onStrip: (id, wire) => {
      strips[id] = wire;
    },
    onError: () => {},
    now: () => now,
    setTimer: (fn, ms) => {
      const t = { fn, at: now + ms, live: true };
      timers.push(t);
      return t;
    },
    clearTimer: (t) => {
      t.live = false;
    },
  });
  async function advance(ms) {
    const end = now + ms;
    for (;;) {
      const next = timers
        .filter((t) => t.live && t.at <= end)
        .sort((a, b) => a.at - b.at)[0];
      if (next === undefined) break;
      next.live = false;
      now = next.at;
      next.fn();
      for (let i = 0; i < 5; i++) await Promise.resolve();
    }
    now = end;
  }
  return { filler, calls, strips, advance };
}

const WIRE = { preview_b64: "AA==", preview_max: 1 };

test("[if] visible rows lack a strip [then] one debounced batch asks for all of them", async () => {
  const h = harness((ids) => ({
    strips: Object.fromEntries(ids.map((i) => [i, WIRE])),
    pending: [],
  }));
  h.filler.setVisible(["a", "b"]);
  h.filler.setVisible(["a", "b", "c"]);
  await h.advance(149);
  assert.equal(h.calls.length, 0, "debounced: nothing before 150 ms");
  await h.advance(1);
  assert.deepEqual(
    h.calls.map((c) => c.ids),
    [["a", "b", "c"]],
  );
  assert.deepEqual(Object.keys(h.strips).sort(), ["a", "b", "c"]);
  await h.advance(60_000);
  assert.equal(
    h.calls.length,
    1,
    "filled ids are never asked again while in view",
  );
});

test("[if] an id stays pending [then] it is re-asked at 2 s, 4 s, 8 s, 15 s and stops when scrolled away", async () => {
  const h = harness((ids) => ({
    strips: Object.fromEntries(ids.map((i) => [i, null])),
    pending: ids,
  }));
  h.filler.setVisible(["p"]);
  await h.advance(150 + 2000 + 4000 + 8000 + 15000 + 15000);
  const gaps = h.calls.slice(1).map((c, i) => c.at - h.calls[i].at);
  assert.deepEqual(gaps, [2000, 4000, 8000, 15000, 15000]);
  h.filler.setVisible([]);
  const before = h.calls.length;
  await h.advance(60_000);
  assert.equal(h.calls.length, before, "scrolled away: no more asks");
});

test("[if] an id is null and not pending [then] it is not asked again while in view", async () => {
  const h = harness((ids) => ({
    strips: Object.fromEntries(ids.map((i) => [i, null])),
    pending: [],
  }));
  h.filler.setVisible(["x"]);
  await h.advance(60_000);
  assert.equal(h.calls.length, 1);
});

test("[if] an id is in flight [then] a second batch never asks for it", () => {
  const due = m.idsDueForStrip(["a", "b", "a"], new Map(), new Set(["a"]), 0);
  assert.deepEqual(due, ["b"]);
  const many = Array.from({ length: 450 }, (_, i) => `id${i}`);
  assert.equal(
    m.idsDueForStrip(many, new Map(), new Set(), 0).length,
    m.STRIP_FILL_MAX_IDS,
  );
});

test("[if] a settled id is reopened [then] it is asked again, and a pending id keeps its backoff", async () => {
  const h = harness((ids) => ({
    strips: Object.fromEntries(ids.map((i) => [i, null])),
    pending: ids.filter((i) => i === "p"),
  }));
  h.filler.setVisible(["x", "p"]);
  await h.advance(150);
  assert.equal(h.calls.length, 1);
  // A library refresh replaced row x: the table reopens it.
  h.filler.reopen(["x", "p"]);
  h.filler.setVisible(["x", "p"]);
  await h.advance(150);
  assert.deepEqual(h.calls[1].ids, ["x"], "x asked again; p still waits out its 2 s backoff");
  // Control: without reopen, a settled id is never asked again.
  h.filler.setVisible(["x", "p"]);
  await h.advance(150);
  assert.equal(h.calls.length, 2);
});
