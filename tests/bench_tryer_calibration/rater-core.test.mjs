// Try-er calibration rater logic (TRYER-24): shuffle, completeness gate, saved payload.
// - if the letter order is not stable for a session+pair, or leaks the manifest order everywhere, then broken
// - if Enter could advance a pair missing a rank or a verdict then broken
// - if the saved payload carries a hidden key field or loses rank/verdict then broken
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const core = require("../../scripts/bench/tryer_calibration/rater-core.js");

const pair = (id, n) => ({ pair_id: id, candidates: Array.from({ length: n }, (_, i) => ({ cid: id + "c" + i, url: "clips/" + id + "c" + i + ".wav" })) });

test("letters are a stable shuffle that does not always keep manifest order", () => {
  const p = pair("p01", 4);
  assert.deepEqual(core.letterMap(p, "s1"), core.letterMap(p, "s1"));
  assert.deepEqual(Object.values(core.letterMap(p, "s1")).sort(), p.candidates.map((c) => c.cid).sort());
  let kept = 0;
  for (let i = 0; i < 40; i++) {
    const q = pair("p" + i, 4);
    const m = core.letterMap(q, "s1");
    if (m.A === q.candidates[0].cid) kept++;
  }
  assert.ok(kept < 20, "first candidate landed on A in " + kept + " of 40 pairs");
  assert.throws(() => core.letterMap(pair("p", 5), "s1"), /2 to 4/);
});

test("a pair is complete only with a full ranking and a verdict per letter", () => {
  const letters = core.letterMap(pair("p01", 3), "s1");
  const st = core.emptyPairState(letters);
  assert.equal(core.missingFor(st, letters).length, 2);
  core.rankNext(st, "B"); core.rankNext(st, "A"); core.rankNext(st, "A");
  assert.deepEqual(st.ranking, ["B", "A"]);
  st.verdict = { A: "ok", B: "good", C: "bad" };
  assert.deepEqual(core.missingFor(st, letters), ["rank C"]);
  core.rankNext(st, "C");
  assert.deepEqual(core.missingFor(st, letters), []);
});

test("payload carries rank, verdict and tags per candidate and nothing from the key", () => {
  const manifest = { session: "s1", pairs: [pair("p01", 3)] };
  const letters = { p01: core.letterMap(manifest.pairs[0], "s1") };
  const st = core.emptyPairState(letters.p01);
  ["C", "A", "B"].forEach((l) => core.rankNext(st, l));
  st.verdict = { A: "ok", B: "bad", C: "good" };
  st.tags.B = ["early cut"];
  const payload = core.buildPayload(manifest, "tryer_calibration/sessions/s1/manifest.json", letters, { p01: st });
  assert.equal(payload.blind_mode, true);
  // Same field set the Python endpoint test posts (tests/bench_tryer_calibration/synthetic.py _entry).
  assert.deepEqual(Object.keys(payload).sort(), ["blind_mode", "manifest", "ratings", "track"]);
  assert.deepEqual(Object.keys(payload.ratings[0]).sort(), ["file", "human_score", "id", "machine_score", "note", "params", "verdict"]);
  assert.equal(payload.ratings.length, 3);
  const byLetter = Object.fromEntries(payload.ratings.map((r) => [JSON.parse(r.note).letter, r]));
  assert.equal(JSON.parse(byLetter.C.note).rank, 1);
  assert.equal(byLetter.C.verdict, "good");
  assert.ok(payload.ratings.every((r) => r.human_score === null), "no number invented from a verdict");
  assert.deepEqual(JSON.parse(byLetter.B.note).tags, ["early cut"]);
  assert.equal(byLetter.B.id, letters.p01.B);
  assert.ok(payload.ratings.every((r) => r.machine_score === null), "no placeholder machine score");
  for (const r of payload.ratings) {
    for (const k of ["role", "style", "score", "features"]) assert.ok(!(k in r) && !(k in JSON.parse(r.note)), k);
  }
  assert.equal(core.completedCount(manifest, letters, { p01: st }), 1);
});

test("autosave is keyed by the manifest build and refuses a different letter map", () => {
  const manifest = { session: "s1", pairs: [pair("p01", 3)] };
  const letters = { p01: core.letterMap(manifest.pairs[0], "s1") };
  assert.notEqual(core.storageKey("s1", "aaa"), core.storageKey("s1", "bbb"));
  const st = { p01: core.emptyPairState(letters.p01) };
  const snap = JSON.stringify(core.snapshot("aaa", letters, st, 0));  // the raw stored text, as the page reads it
  assert.equal(core.checkRestore(snap, "aaa", letters, 1).ok, true);
  assert.match(core.checkRestore(snap, "bbb", letters, 1).reason, /different build/);
  const rebuilt = { p01: { ...letters.p01, A: "other-clip" } };
  assert.match(core.checkRestore(snap, "aaa", rebuilt, 1).reason, /letter-to-clip/);
  assert.equal(core.checkRestore(null, "aaa", letters, 1).empty, true);
});

test("[if] a same-build snapshot is malformed in any field [then] checkRestore refuses it with a reason, never a partial or coerced restore", () => {
  const manifest = { pairs: [pair("p1", 2)] };
  const letters = { p1: core.letterMap(manifest.pairs[0], 3) };
  const good = core.emptyPairState(letters.p1);
  const base = (over) => Object.assign(core.snapshot("aaa", letters, { p1: good }, 0), over);
  assert.equal(core.checkRestore(JSON.stringify(base({})), "aaa", letters, 1).ok, true, "control: a well-formed snapshot restores");
  const firstTwo = Object.keys(letters.p1);
  const bad = [
    base({ pairs: undefined }),
    base({ pairs: "x" }),
    base({ pairs: { p1: "x" } }),
    base({ pairs: { p1: { verdict: {}, tags: good.tags } } }),
    base({ pairs: { p1: { ranking: ["Z"], verdict: {}, tags: good.tags } } }),
    base({ pairs: { p1: { ranking: [], verdict: { A: "great" }, tags: good.tags } } }),
    base({ pairs: { p1: { ranking: [], verdict: {}, tags: { A: "x" } } } }),
    base({ pairs: { p1: { ranking: [], verdict: {}, tags: { A: ["not-a-tag"] } } } }),
    base({ pairs: { zz: good } }),
    base({ index: 5 }),
    base({ index: "0" }),
    base({ pairs: { p1: { ranking: [firstTwo[0], firstTwo[0]], verdict: {}, tags: good.tags } } }),
    base({ pairs: { p1: { ranking: [], verdict: {}, tags: { [firstTwo[0]]: [] } } } }),
    base({ pairs: { p1: { ranking: [], verdict: [], tags: good.tags } } }),
    base({ pairs: { p1: { ranking: [], verdict: {}, tags: [] } } }),
    base({ pairs: { p1: { ...good, extra: 1 } } }),
    base({ extra: 1 }),
  ];
  assert.match(core.checkRestore("{not json", "aaa", letters, 1).reason, /not valid JSON/);
  for (const snap of bad) {
    const checked = core.checkRestore(JSON.stringify(snap), "aaa", letters, 1);
    assert.equal(checked.ok, false, JSON.stringify(snap));
    assert.ok(checked.reason && checked.reason.length > 0);
  }
});

test("[if] the page finds autosaves from earlier builds or a refused snapshot [then] none is deleted; a refused one is kept under a quarantine key", () => {
  const keys = ["tcr:s:old1", "tcr:s:now", "tcr:other:x", "tcr:s"];
  assert.deepEqual(core.earlierBuildKeys(keys, "s", "tcr:s:now").sort(), ["tcr:s", "tcr:s:old1"]);
  const q = core.quarantineKey("tcr:s:now", 1700000000000);
  assert.ok(q.startsWith("tcr-quarantine:s:now:") && q !== "tcr:s:now", q);
  assert.equal(core.earlierBuildKeys([q], "s", "tcr:s:now").length, 0, "a quarantined copy is not read as a build");
});

test("[if] autosave is off [then] a successful Save or any change leaves the warning up, [else stop]", () => {
  assert.equal(core.bannerAfterSave(false), "", "control: with autosave on, a Save clears the banner");
  assert.match(core.bannerAfterSave(true), /Autosave is OFF/);
});
