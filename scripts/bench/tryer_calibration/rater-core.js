/*
 * Pure logic of the try-er calibration rater (TRYER-24): shuffle, completeness,
 * the saved payload. No DOM here, so Node's built-in test runner loads it as is.
 * Loaded by rater.html as a classic script (window.RaterCore) and by the test
 * through module.exports.
 */
(function (root) {
  "use strict";

  const LETTERS = ["A", "B", "C", "D"];
  // Shown on every rating change while autosave is off: the change exists only on this page until Save.
  const AUTOSAVE_OFF_WARNING = "Autosave is OFF: this change is NOT kept in this browser. Press Save before you leave or reload.";
  const VERDICTS = ["good", "ok", "bad"];
  const TAGS = [
    "beat clash",
    "key clash",
    "timbre clash",
    "energy drop",
    "too long",
    "too short",
    "effect gimmick",
    "early cut",
    "loudness jump",
    "awkward mix point",
    "contrasting songs",
  ];

  /** The banner after a save or a change: the autosave-off warning stays up for as long as autosave is off. */
  function bannerAfterSave(autosaveOff) {
    return autosaveOff ? AUTOSAVE_OFF_WARNING : "";
  }

  /** FNV-1a 32-bit: a stable seed from a string. */
  function hashSeed(text) {
    let h = 0x811c9dc5;
    for (let i = 0; i < text.length; i++) {
      h ^= text.charCodeAt(i);
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    return h >>> 0;
  }

  /** mulberry32 PRNG. */
  function rng(seed) {
    let a = seed >>> 0;
    return function () {
      a = (a + 0x6d2b79f5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  /** Deterministic Fisher-Yates over candidate ids, seeded by session + pair. */
  function shuffleCandidates(cids, seedText) {
    const out = cids.slice();
    const next = rng(hashSeed(seedText));
    for (let i = out.length - 1; i > 0; i--) {
      const j = Math.floor(next() * (i + 1));
      [out[i], out[j]] = [out[j], out[i]];
    }
    return out;
  }

  /** letter -> cid for one pair. */
  function letterMap(pair, session) {
    const cids = pair.candidates.map((c) => c.cid);
    if (cids.length < 2 || cids.length > LETTERS.length) {
      throw new Error("pair " + pair.pair_id + " has " + cids.length + " candidates; the page rates 2 to 4");
    }
    const order = shuffleCandidates(cids, session + "/" + pair.pair_id);
    const map = {};
    order.forEach((cid, i) => { map[LETTERS[i]] = cid; });
    return map;
  }

  function emptyPairState(letters) {
    return { ranking: [], verdict: {}, tags: Object.fromEntries(Object.keys(letters).map((l) => [l, []])) };
  }

  /** What is still missing before Enter may advance. */
  function missingFor(pairState, letters) {
    const all = Object.keys(letters);
    const missing = [];
    const unranked = all.filter((l) => !pairState.ranking.includes(l));
    if (unranked.length > 0) missing.push("rank " + unranked.join(", "));
    const unrated = all.filter((l) => !VERDICTS.includes(pairState.verdict[l]));
    if (unrated.length > 0) missing.push("verdict for " + unrated.join(", "));
    return missing;
  }

  /** Append a letter to the ranking (best first); a letter already ranked is ignored. */
  function rankNext(pairState, letter) {
    if (!pairState.ranking.includes(letter)) pairState.ranking.push(letter);
    return pairState;
  }

  /** The artifact POST /api/v1/bench/ratings accepts (and the download carries). */
  function buildPayload(manifest, manifestRel, letterMaps, states) {
    const ratings = [];
    for (const pair of manifest.pairs) {
      const letters = letterMaps[pair.pair_id];
      const st = states[pair.pair_id];
      const urlOf = Object.fromEntries(pair.candidates.map((c) => [c.cid, c.url]));
      for (const letter of Object.keys(letters)) {
        const cid = letters[letter];
        const rankIndex = st ? st.ranking.indexOf(letter) : -1;
        const verdict = st ? st.verdict[letter] || null : null;
        ratings.push({
          id: cid,
          file: urlOf[cid],
          params: "tryer-calibration v1; blind",
          machine_score: null,
          // The listener gives a verdict, never a 1..10 number, so human_score stays null rather than invented.
          human_score: null,
          verdict: verdict,
          // The verdict lives only in its own field above: one copy, so the server and the fit never disagree.
          note: JSON.stringify({
            pair_id: pair.pair_id,
            letter: letter,
            rank: rankIndex >= 0 ? rankIndex + 1 : null,
            tags: st ? st.tags[letter] || [] : [],
          }),
        });
      }
    }
    return { track: "tryer-calibration-" + manifest.session, blind_mode: true, manifest: manifestRel, ratings: ratings };
  }

  /** localStorage key bound to the exact manifest build (its sha256), so a rebuild never meets old progress. */
  function storageKey(session, manifestSha256) {
    return "tcr:" + session + ":" + manifestSha256;
  }

  /** What autosave stores: progress plus the letter-to-clip map it was made against. */
  function snapshot(manifestSha256, letterMaps, states, index) {
    return { manifest_sha256: manifestSha256, letters: letterMaps, pairs: states, index: index };
  }

  /**
   * Validate a stored snapshot against the loaded manifest.
   * Returns {ok: true, pairs, index} or {ok: false, reason}; never a partial restore.
   */
  function checkRestore(stored, manifestSha256, letterMaps, pairCount) {
    // ``stored`` is the raw localStorage text (or null): an unreadable snapshot is refused like any other,
    // so the page sets it aside before autosave can overwrite it.
    if (stored === null) return { ok: true, pairs: {}, index: 0, empty: true };
    let saved;
    try {
      saved = JSON.parse(stored);
    } catch (err) {
      return { ok: false, reason: "saved progress is not valid JSON (" + err.name + ": " + err.message + ")" };
    }
    if (!isPlainObject(saved) || saved.manifest_sha256 !== manifestSha256) {
      return { ok: false, reason: "saved progress belongs to a different build of this session" };
    }
    const fields = Object.keys(saved).sort().join(",");
    if (fields !== "index,letters,manifest_sha256,pairs") {
      return { ok: false, reason: "saved progress is damaged: it has the fields " + fields };
    }
    if (JSON.stringify(saved.letters) !== JSON.stringify(letterMaps)) {
      return { ok: false, reason: "saved progress was made against a different letter-to-clip map" };
    }
    if (!Number.isInteger(saved.index) || saved.index < 0 || saved.index >= pairCount) {
      return { ok: false, reason: "saved progress has an invalid pair index " + JSON.stringify(saved.index) };
    }
    const shape = pairsShapeError(saved.pairs, letterMaps);
    if (shape) return { ok: false, reason: "saved progress is damaged: " + shape };
    return { ok: true, pairs: saved.pairs, index: saved.index, empty: false };
  }

  function isPlainObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  /** Why a restored pairs object does not have the exact shape the page renders, or "" when it does. */
  function pairsShapeError(pairs, letterMaps) {
    if (!isPlainObject(pairs)) return "pairs is not an object";
    for (const [pairId, st] of Object.entries(pairs)) {
      const letters = Object.hasOwn(letterMaps, pairId) ? letterMaps[pairId] : null;
      if (!letters) return "pair " + pairId + " is not in this build";
      if (!isPlainObject(st)) return "pair " + pairId + " is not an object";
      const keys = Object.keys(st).sort().join(",");
      if (keys !== "ranking,tags,verdict") return "pair " + pairId + " has fields " + keys;
      if (!Array.isArray(st.ranking) || !st.ranking.every((l) => Object.hasOwn(letters, l))) return "pair " + pairId + " ranking";
      if (new Set(st.ranking).size !== st.ranking.length) return "pair " + pairId + " ranking repeats a letter";
      if (!isPlainObject(st.verdict)) return "pair " + pairId + " verdict";
      for (const [l, v] of Object.entries(st.verdict)) {
        if (!Object.hasOwn(letters, l) || !VERDICTS.includes(v)) return "pair " + pairId + " verdict " + l;
      }
      if (!isPlainObject(st.tags)) return "pair " + pairId + " tags";
      if (Object.keys(st.tags).sort().join(",") !== Object.keys(letters).sort().join(",")) return "pair " + pairId + " tags must name every letter";
      for (const [l, tags] of Object.entries(st.tags)) {
        if (!Array.isArray(tags) || !tags.every((t) => TAGS.includes(t))) return "pair " + pairId + " tags " + l;
      }
    }
    return "";
  }

  /** Autosave keys of this session's EARLIER builds (never the current one, never another session's). */
  function earlierBuildKeys(keys, session, currentKey) {
    return keys.filter((k) => (k === "tcr:" + session || k.startsWith("tcr:" + session + ":")) && k !== currentKey);
  }

  /** Where a refused snapshot is moved instead of being deleted: outside the tcr: namespace the page reads. */
  function quarantineKey(currentKey, nowMs) {
    return "tcr-quarantine:" + currentKey.slice("tcr:".length) + ":" + nowMs;
  }

  function completedCount(manifest, letterMaps, states) {
    return manifest.pairs.filter((p) => states[p.pair_id] && missingFor(states[p.pair_id], letterMaps[p.pair_id]).length === 0).length;
  }

  const api = { AUTOSAVE_OFF_WARNING, bannerAfterSave, storageKey, snapshot, checkRestore, earlierBuildKeys, quarantineKey, LETTERS, VERDICTS, TAGS, hashSeed, shuffleCandidates, letterMap, emptyPairState, missingFor, rankNext, buildPayload, completedCount };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.RaterCore = api;
})(typeof window !== "undefined" ? window : globalThis);
