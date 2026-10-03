import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Adapted from the af--karaoke-ui-deck spike. The spike derived per-line
// fidelity from local score heuristics (median / shaky-fraction / hysteresis);
// the daemon now serves canonical lines with a CALIBRATED witness `band`, so
// build-track.ts is an ADAPTER and this suite pins its mapping and its
// fail-fast contract instead.
//
// Regression lines:
// - if a band 'good' line does not get fidelity 'word' then the wipe is lost
// - if 'uncertain', 'bad' or 'unjudged' gets fidelity 'word' then the strip
//   animates confidently over words the witness distrusts (the one failure a
//   DJ never forgives)
// - if word_fidelity_lines counts non-good lines then the MIX summary lies
// - if an untimed word survives into the cursor's word list then the cursor
//   places a word that has no clock position
// - if dropping untimed words changes a line's server TEXT then the display
//   invents a lyric edit
// - if lines fail to partition the timed words and the adapter stays silent
//   then buildWordLineMap attributes words to the WRONG line (a silent lie)
// - if a payload without lines, without timings, with a zero span or with
//   non-monotonic onsets adapts without throwing then fail-fast is broken

let build;
let cursor;

// pred-r4c-shaped output for 100000002 (Hollis Wren / Odette Marsh - Sunday
// Clay, Kestrel Hale Remix), words 253..283 (6 server lines). Timings and scores are
// aligner output; the word text is invented.
const FIXTURE_WORDS = [
	{ word: 'We', start_s: 145.4, end_s: 145.48, score: -3.7842, line_final: false }, // #253
	{ word: 'watch', start_s: 145.6, end_s: 145.8, score: -0.0413, line_final: false }, // #254
	{ word: 'the', start_s: 145.82, end_s: 145.9, score: -4.9606, line_final: false }, // #255
	{ word: 'harbor', start_s: 145.94, end_s: 146.1, score: -2.0969, line_final: false }, // #256
	{ word: 'lights', start_s: 146.16, end_s: 146.6, score: -0.6512, line_final: true }, // #257
	{ word: 'The', start_s: 146.72, end_s: 147.64, score: -0.2971, line_final: false }, // #258
	{ word: 'tide', start_s: 147.7, end_s: 148.1, score: -1.3169, line_final: false }, // #259
	{ word: 'keeps', start_s: 148.12, end_s: 148.2, score: -5.9453, line_final: false }, // #260
	{ word: 'turning', start_s: 148.32, end_s: 148.48, score: -1.6107, line_final: false }, // #261
	{ word: 'slow', start_s: 148.58, end_s: 148.74, score: -0.8976, line_final: false }, // #262
	{ word: 'now', start_s: 148.8, end_s: 148.96, score: -1.7357, line_final: true }, // #263
	{ word: 'Leave', start_s: 149.3, end_s: 149.68, score: -1.5598, line_final: false }, // #264
	{ word: 'the', start_s: 149.72, end_s: 149.84, score: -1.707, line_final: false }, // #265
	{ word: 'paper', start_s: 149.9, end_s: 149.96, score: -3.824, line_final: false }, // #266
	{ word: 'lanterns', start_s: 150.1, end_s: 150.82, score: -0.8843, line_final: true }, // #267
	{ word: 'She', start_s: 151.34, end_s: 151.44, score: -1.4684, line_final: false }, // #268
	{ word: 'folds', start_s: 151.56, end_s: 151.78, score: -1.1335, line_final: false }, // #269
	{ word: 'the', start_s: 151.8, end_s: 151.88, score: -3.322, line_final: false }, // #270
	{ word: 'map', start_s: 151.94, end_s: 152.78, score: -0.2282, line_final: true }, // #271
	{ word: 'She', start_s: 153.12, end_s: 153.22, score: -0.7441, line_final: false }, // #272
	{ word: "can't", start_s: 153.28, end_s: 153.46, score: -1.4807, line_final: false }, // #273
	{ word: 'name', start_s: 153.52, end_s: 153.7, score: -0.3745, line_final: false }, // #274
	{ word: 'the', start_s: 153.76, end_s: 153.84, score: -1.3858, line_final: false }, // #275
	{ word: 'season', start_s: 153.9, end_s: 154.36, score: -0.4233, line_final: false }, // #276
	{ word: 'yet', start_s: 154.38, end_s: 154.58, score: -0.6194, line_final: true }, // #277
	{ word: "That's", start_s: 155.22, end_s: 155.44, score: -2.0336, line_final: false }, // #278
	{ word: 'where', start_s: 155.48, end_s: 155.6, score: -2.6422, line_final: false }, // #279
	{ word: "we're", start_s: 155.62, end_s: 155.76, score: -3.9207, line_final: false }, // #280
	{ word: 'heading', start_s: 155.82, end_s: 156.22, score: -0.4668, line_final: false }, // #281
	{ word: 'for', start_s: 156.3, end_s: 156.5, score: -0.9205, line_final: false }, // #282
	{ word: 'dawn', start_s: 156.56, end_s: 156.7, score: -0.9684, line_final: true } // #283
];

/** One of each band across the 6 lines, so every mapping rule is exercised. */
const BANDS = ['bad', 'good', 'uncertain', 'good', 'good', 'unjudged'];

const META = {
	id: '100000002',
	artist: 'Hollis Wren,Odette Marsh,Kestrel Hale',
	title: '2A - 6 - Sunday Clay - Kestrel Hale Remix',
	duration_s: 326
};

// ------------------------------------------------- API payload construction

function apiWordsFrom(words) {
	return words.map((w, i) => ({
		idx: i,
		word: w.word,
		start_s: w.start_s,
		end_s: w.end_s,
		score: w.score,
		witness: 'agree',
		line_final: w.line_final
	}));
}

function apiLinesFrom(words, bands) {
	const lines = [];
	let first = 0;
	for (let i = 0; i < words.length; i += 1) {
		const isLast = i === words.length - 1;
		if (!words[i].line_final && !isLast) continue;
		const slice = words.slice(first, i + 1);
		const band = bands[lines.length];
		const judged = band === 'unjudged' ? 0 : slice.length;
		const red = band === 'bad' ? Math.max(1, Math.floor(slice.length / 2)) : 0;
		lines.push({
			first_idx: first,
			last_idx: i,
			text: slice.map((w) => w.word).join(' '),
			start_s: slice[0].start_s,
			end_s: slice[slice.length - 1].end_s,
			n_words: slice.length,
			n_red: red,
			n_judged: judged,
			quality: judged === 0 ? null : (judged - red) / judged,
			band,
			para_final: false
		});
		first = i + 1;
	}
	if (lines.length !== bands.length) {
		throw new Error(`fixture drift: ${lines.length} lines, ${bands.length} bands`);
	}
	return lines;
}

function apiTrackFrom(words, { bands = BANDS, verdict = 'vocal', lines } = {}) {
	return {
		verdict: {
			stable_id: META.id,
			verdict,
			effective: verdict,
			coverage_pct: 61.0,
			source: 'test-fixture',
			language_iso3: 'eng',
			n_words: words.length,
			pct_witness_red: 10.0,
			override: null,
			override_note: null,
			computed_at: '2026-08-31T00:00:00Z',
			updated_at: '2026-08-31T00:00:00Z'
		},
		words: apiWordsFrom(words),
		lines: lines === undefined ? apiLinesFrom(words, bands) : lines
	};
}

let track;

before(async () => {
	build = await loadTypeScriptModule('src/lib/rb/lyrics/build-track.ts');
	cursor = await loadTypeScriptModule('src/lib/rb/lyrics/cursor.ts');
	track = build.adaptLyricTrack(META, apiTrackFrom(FIXTURE_WORDS));
});

// ------------------------------------------------------ band -> fidelity

test('the fixture reads six lines carrying all four bands', () => {
	assert.equal(track.lines.length, 6);
	assert.deepEqual(
		track.lines.map((l) => l.band),
		['bad', 'good', 'uncertain', 'good', 'good', 'unjudged'],
		'fixture no longer exercises every band'
	);
});

test('only band good earns per-word fidelity; every other band lights whole', () => {
	assert.deepEqual(
		track.lines.map((l) => l.fidelity),
		['line', 'word', 'line', 'word', 'word', 'line']
	);
});

test('the server band replaces the spike score heuristics entirely', () => {
	// Line 1 ("The tide keeps turning slow now") contains a -5.95 score word;
	// the spike's shaky-fraction rule would have demoted it. The witness says
	// GOOD, and the witness is calibrated - the wipe must survive.
	assert.equal(track.lines[1].band, 'good');
	assert.equal(track.lines[1].fidelity, 'word');
});

test('word_fidelity_lines counts exactly the good lines', () => {
	assert.equal(track.word_fidelity_lines, 3);
});

test('band_counts summarises the mix for the tooltip', () => {
	assert.deepEqual(track.band_counts, { good: 3, uncertain: 1, bad: 1, unjudged: 1 });
});

test('quality, n_red and n_judged pass through for the line titles', () => {
	const bad = track.lines[0];
	assert.equal(bad.n_judged, 5);
	assert.equal(bad.n_red, 2);
	assert.equal(bad.quality, 3 / 5);
	const unjudged = track.lines[5];
	assert.equal(unjudged.n_judged, 0);
	assert.equal(unjudged.quality, null, 'unjudged is not good: quality must stay null');
});

test('verdict and language flow through from the API verdict', () => {
	assert.equal(track.verdict, 'vocal');
	assert.equal(track.language_iso, 'eng');
});

// ------------------------------------------------------- untimed words

test('untimed words are dropped from the cursor domain but keep the line text', () => {
	const words = FIXTURE_WORDS.map((w) => ({ ...w }));
	// "late" (#260, mid line 1) loses its timing, as unaligned words do.
	words[7] = { ...words[7], start_s: null, end_s: null };
	const t = build.adaptLyricTrack(META, apiTrackFrom(words));
	assert.equal(t.words.length, FIXTURE_WORDS.length - 1);
	assert.equal(
		t.words.some((w) => w.src_idx === 7),
		false,
		'the untimed word leaked into the cursor word list'
	);
	assert.equal(
		t.lines[1].text,
		"The tide keeps turning slow now",
		'the server line text must survive the drop untouched'
	);
	// The line still spans its remaining timed words.
	assert.equal(t.lines[1].last_word - t.lines[1].first_word + 1, 5);
});

test('a fully untimed line is dropped and its neighbours still partition', () => {
	const words = FIXTURE_WORDS.map((w, i) =>
		i >= 11 && i <= 14 ? { ...w, start_s: null, end_s: null } : { ...w }
	); // line 2 ("Leave the paper lanterns") entirely untimed
	const t = build.adaptLyricTrack(META, apiTrackFrom(words));
	assert.equal(t.lines.length, 5);
	assert.deepEqual(
		t.lines.map((l) => l.band),
		['bad', 'good', 'good', 'good', 'unjudged']
	);
	// Every timed word maps to exactly one line, in order, no gaps.
	const map = cursor.buildWordLineMap(t);
	for (let i = 0; i < t.words.length; i += 1) {
		const line = t.lines[map[i]];
		assert.equal(i >= line.first_word && i <= line.last_word, true, `word ${i} mapped off-line`);
	}
	assert.equal(t.lines[0].first_word, 0);
	assert.equal(t.lines[t.lines.length - 1].last_word, t.words.length - 1);
});

test('line indexes are re-sequenced after a drop so the cursor map stays dense', () => {
	const words = FIXTURE_WORDS.map((w, i) =>
		i >= 11 && i <= 14 ? { ...w, start_s: null, end_s: null } : { ...w }
	);
	const t = build.adaptLyricTrack(META, apiTrackFrom(words));
	assert.deepEqual(
		t.lines.map((l) => l.index),
		[0, 1, 2, 3, 4]
	);
});

// --------------------------------------------------------- fail-fast

test('a payload without lines throws (fetch with include=lines)', () => {
	assert.throws(
		() => build.adaptLyricTrack(META, apiTrackFrom(FIXTURE_WORDS, { lines: null })),
		/no lines/
	);
});

test('a payload with zero timed words throws', () => {
	const words = FIXTURE_WORDS.map((w) => ({ ...w, start_s: null, end_s: null }));
	assert.throws(() => build.adaptLyricTrack(META, apiTrackFrom(words)), /no word timings/);
});

test('a zero-span word throws', () => {
	const words = FIXTURE_WORDS.map((w) => ({ ...w }));
	words[3] = { ...words[3], end_s: words[3].start_s };
	assert.throws(() => build.adaptLyricTrack(META, apiTrackFrom(words)), /zero or negative span/);
});

test('a non-monotonic onset throws', () => {
	const words = FIXTURE_WORDS.map((w) => ({ ...w }));
	words[10] = { ...words[10], start_s: 100.0, end_s: 100.5 };
	assert.throws(
		() => build.adaptLyricTrack(META, apiTrackFrom(words)),
		/starts before its predecessor/
	);
});

test('a non-finite timing throws', () => {
	const words = FIXTURE_WORDS.map((w) => ({ ...w }));
	words[3] = { ...words[3], end_s: Number.NaN };
	assert.throws(() => build.adaptLyricTrack(META, apiTrackFrom(words)), /non-finite/);
});

test('lines that do not partition the timed words throw instead of mis-attributing', () => {
	// Drop the middle server line while its words stay timed: those words
	// would silently land on line 0 of the word-line map.
	const api = apiTrackFrom(FIXTURE_WORDS);
	api.lines = api.lines.filter((l) => l.first_idx !== 11);
	assert.throws(() => build.adaptLyricTrack(META, api), /do not partition/);
});

test('timed words past the last line throw instead of vanishing', () => {
	const api = apiTrackFrom(FIXTURE_WORDS);
	api.lines = api.lines.slice(0, -1);
	assert.throws(() => build.adaptLyricTrack(META, api), /do not partition/);
});

// ------------------------------------------------------- determinism

test('the adapter is pure: two runs over the same payload agree exactly', () => {
	const again = build.adaptLyricTrack(META, apiTrackFrom(FIXTURE_WORDS));
	assert.deepEqual(again, track);
});
