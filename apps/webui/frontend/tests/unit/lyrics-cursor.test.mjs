import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Ported from the af--karaoke-ui-deck spike (its clock discipline is the
// hard-won part and is kept verbatim); fixtures adapted from the spike's raw
// word arrays to the daemon's /api/v1/tracks/{id}/lyrics payload shape, which
// is what build-track.ts now adapts.
//
// Deck karaoke clock contract. The defect this pins: a long SUSTAINED word
// ("ooh-ooh-ooh-ooh" held for 29.4 seconds) is still sounding, but any code
// that computes "time to next vocal" as nextWord.start_s - now, without first
// asking whether a word is ACTIVE, announces a ~25 second countdown over the
// top of the singing. The maintainer hit exactly this on 100000001.
//
// THE INVARIANT: while the playhead is inside a word's half-open
// [start_s, end_s), the state can never be gap/instrumental and no countdown
// may exist. A countdown is only meaningful when NO word is active, and it is
// measured from the playhead to the next onset - never from the start of a
// word still in progress.
//
// Regression lines:
// - if a countdown shows while a word is active then broken
// - if the state goes gap/instrumental while a word is active then broken
// - if a LyricsFrame carries next_vocal_in_ms with a non-null word_index then broken
// - if the playhead at exactly end_s still reports that word active then the
//   interval stopped being half-open
// - if a countdown after a word ends is measured from anything but the
//   playhead to the next onset then broken
// - if a normal sub-second inter-word gap is called an instrumental then broken
// - if a real 6.28s silence is NOT called an instrumental then broken
// - if a sample landing exactly ON a word's start_s reports no active word then
//   the interval stopped being start-INCLUSIVE (a one-sample phantom gap)
// - if a sample where one line ends on the exact instant the next begins falls
//   through both lines then the boundary has a hole

let cursor;
let build;
let track;
let wordLine;

// pred-r4c-shaped output for 100000001 (Ivory Lake / KESTREL - Hold On), words
// 30..56. Local index = pred-r4c index - 30. The pathological spans (timings,
// scores, line_final) are those the aligner actually emitted; the word text is invented.
const REAL_WORDS = [
	{ word: 'Ooh-ooh,', start_s: 132.06, end_s: 136.16, score: -0.1246, line_final: false }, // #30
	{ word: 'ooh-ooh', start_s: 136.22, end_s: 140.4, score: -0.1445, line_final: false }, // #31
	{ word: 'we', start_s: 140.6, end_s: 140.68, score: -4.3237, line_final: false }, // #32
	{ word: 'hold', start_s: 141.28, end_s: 142.04, score: -0.3815, line_final: false }, // #33
	{ word: 'on', start_s: 142.1, end_s: 142.28, score: -0.8207, line_final: false }, // #34
	{ word: 'is', start_s: 142.34, end_s: 143.12, score: -0.2421, line_final: false }, // #35
	{ word: 'luminous', start_s: 149.4, end_s: 150.18, score: -1.3141, line_final: true }, // #36
	{ word: 'Ooh', start_s: 150.26, end_s: 150.64, score: -0.6003, line_final: false }, // #37
	{ word: 'ooh-ooh,', start_s: 150.74, end_s: 151.14, score: -1.137, line_final: false }, // #38
	{ word: 'ooh-ooh', start_s: 151.18, end_s: 154.92, score: -0.1538, line_final: false }, // #39
	{ word: 'we', start_s: 154.94, end_s: 155.0, score: -5.1041, line_final: false }, // #40
	{ word: 'hold', start_s: 155.04, end_s: 155.2, score: -2.8738, line_final: false }, // #41
	{ word: "we'll", start_s: 155.22, end_s: 155.32, score: -4.3988, line_final: false }, // #42
	{ word: 'as', start_s: 155.38, end_s: 155.72, score: -0.979, line_final: false }, // #43
	{ word: 'thunder', start_s: 155.84, end_s: 156.36, score: -1.6026, line_final: true }, // #44
	{ word: 'Hey', start_s: 156.38, end_s: 156.48, score: -3.1427, line_final: false }, // #45
	{ word: 'now,', start_s: 156.52, end_s: 156.7, score: -0.9154, line_final: false }, // #46
	{ word: 'now', start_s: 156.74, end_s: 156.98, score: -0.5237, line_final: true }, // #47
	{ word: 'Ooh-ooh-ooh,', start_s: 157.32, end_s: 157.76, score: -1.9703, line_final: false }, // #48
	{ word: 'ooh,', start_s: 157.78, end_s: 158.02, score: -0.8255, line_final: false }, // #49
	{ word: 'ooh-ooh-ooh-ooh', start_s: 158.06, end_s: 159.56, score: -0.6974, line_final: true }, // #50
	{ word: 'Ooh-ooh-ooh,', start_s: 164.28, end_s: 165.7, score: -0.5852, line_final: false }, // #51
	{ word: 'ooh,', start_s: 165.76, end_s: 165.94, score: -1.392, line_final: false }, // #52
	{ word: 'ooh-ooh-ooh-ooh', start_s: 166.02, end_s: 195.44, score: -0.0422, line_final: true }, // #53
	{ word: 'Hey', start_s: 195.48, end_s: 195.66, score: -1.5114, line_final: false }, // #54
	{ word: 'now', start_s: 195.68, end_s: 195.88, score: -0.9003, line_final: true }, // #55
	{ word: 'Lanterns', start_s: 198.88, end_s: 199.26, score: -1.0446, line_final: false } // #56
];

/** The 29.4 second sustained word, local index 23 (pred-r4c #53). */
const SUSTAINED = 23;
/** Local index 5 ("is", pred-r4c #35): the word before a real 6.28s silence. */
const BEFORE_SILENCE = 5;

const META = {
	id: '100000001',
	artist: 'Ivory Lake,KESTREL',
	title: '8B - 6 - Hold On - KESTREL Remix',
	duration_s: 351
};

// ------------------------------------------------- API payload construction
// The daemon serves words with idx/witness and SERVER-side canonical lines;
// these helpers wrap the raw aligner fixture into that payload shape exactly
// (grouping on line_final, the same rule apps/lyrics/lines.py applies).

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

function apiLinesFrom(words) {
	const lines = [];
	let first = 0;
	for (let i = 0; i < words.length; i += 1) {
		const isLast = i === words.length - 1;
		if (!words[i].line_final && !isLast) continue;
		const slice = words.slice(first, i + 1);
		lines.push({
			first_idx: first,
			last_idx: i,
			text: slice.map((w) => w.word).join(' '),
			start_s: slice[0].start_s,
			end_s: slice[slice.length - 1].end_s,
			n_words: slice.length,
			n_red: 0,
			n_judged: slice.length,
			quality: 1,
			band: 'good',
			para_final: false
		});
		first = i + 1;
	}
	return lines;
}

function apiTrackFrom(words) {
	return {
		verdict: {
			stable_id: META.id,
			verdict: 'vocal',
			effective: 'vocal',
			coverage_pct: 55.0,
			source: 'test-fixture',
			language_iso3: 'eng',
			n_words: words.length,
			pct_witness_red: 0,
			override: null,
			override_note: null,
			computed_at: '2026-08-31T00:00:00Z',
			updated_at: '2026-08-31T00:00:00Z'
		},
		words: apiWordsFrom(words),
		lines: apiLinesFrom(words)
	};
}

function at(t) {
	return cursor.resolveCursor(track, wordLine, t, 0);
}

/** resolveCursor returns raw float ms; only toLyricsFrame rounds. */
function assertMs(actual, expected, message) {
	assert.equal(
		Math.abs(actual - expected) < 1,
		true,
		`${message}: expected ~${expected}ms, got ${actual}`
	);
}

function frameAt(t) {
	return cursor.toLyricsFrame(track, at(t), t, 'word');
}

before(async () => {
	cursor = await loadTypeScriptModule('src/lib/rb/lyrics/cursor.ts');
	build = await loadTypeScriptModule('src/lib/rb/lyrics/build-track.ts');
	track = build.adaptLyricTrack(META, apiTrackFrom(REAL_WORDS));
	wordLine = cursor.buildWordLineMap(track);
});

// ------------------------------------------------- the 29.4 second word

test('a 29.4s sustained word stays the active word all the way through', () => {
	const word = REAL_WORDS[SUSTAINED];
	assert.equal(word.end_s - word.start_s > 29, true, 'fixture lost the long word');
	for (let t = word.start_s; t < word.end_s; t += 0.5) {
		const c = at(t);
		assert.equal(c.wordIndex, SUSTAINED, `no active word at t=${t.toFixed(2)}`);
		assert.equal(c.state, 'line', `state became ${c.state} at t=${t.toFixed(2)}`);
	}
});

test('no countdown exists anywhere inside the 29.4s sustained word', () => {
	const word = REAL_WORDS[SUSTAINED];
	for (let t = word.start_s; t < word.end_s; t += 0.25) {
		const c = at(t);
		assert.equal(
			c.nextVocalInMs,
			null,
			`countdown ${c.nextVocalInMs}ms announced while the word was still sounding at t=${t.toFixed(2)}`
		);
	}
});

test('the naive next.start_s - now countdown is never emitted mid-word', () => {
	// t=170 sits 4s into the sustained word. A naive implementation reports
	// (195.48 - 170) * 1000 = 25480ms of "silence" over the top of the singing.
	const c = at(170);
	assert.equal(c.wordIndex, SUSTAINED);
	assert.equal(c.state, 'line');
	assert.equal(c.nextVocalInMs, null);
});

// ---------------------------------------------- half-open interval edges

test('the exact end_s of the sustained word is NOT active', () => {
	const c = at(REAL_WORDS[SUSTAINED].end_s);
	assert.equal(c.wordIndex, null, 'end_s must be excluded: intervals are half-open');
	assert.equal(c.state, 'line', 'a 0.04s gap is not an instrumental');
});

test('the exact start_s of a word IS active', () => {
	const c = at(REAL_WORDS[SUSTAINED].start_s);
	assert.equal(c.wordIndex, SUSTAINED);
	assert.equal(c.nextVocalInMs, null);
});

test('just past the sustained word the countdown is measured from the playhead', () => {
	const t = 195.45; // 0.01s past end_s 195.44, next onset 195.48
	const c = at(t);
	assert.equal(c.wordIndex, null);
	assertMs(c.nextVocalInMs, (195.48 - t) * 1000, 'countdown must run from the playhead');
	assertMs(c.nextVocalInMs, 30, 'countdown must be the real 30ms silence');
});

// ------------------------------------------------------ ordinary gaps

test('a 0.6s inter-word gap has no active word and is not an instrumental', () => {
	const t = 140.9; // between "you" end 140.68 and "know" start 141.28
	const c = at(t);
	assert.equal(c.wordIndex, null, 'a word must not be stretched over the gap it precedes');
	assert.equal(c.state, 'line');
	assertMs(c.nextVocalInMs, (141.28 - t) * 1000, 'countdown must run from the playhead');
});

test('a real 6.28s silence is called an instrumental with a countdown', () => {
	const t = 144; // "is" ended 143.12, "frightening" starts 149.4
	const c = at(t);
	assert.equal(c.wordIndex, null);
	assert.equal(c.state, 'gap');
	assertMs(c.nextVocalInMs, (149.4 - t) * 1000, 'countdown must run from the playhead');
});

test('the word before that silence is active right up to its own end_s', () => {
	const word = REAL_WORDS[BEFORE_SILENCE];
	const c = at(word.end_s - 0.001);
	assert.equal(c.wordIndex, BEFORE_SILENCE);
	assert.equal(c.state, 'line');
	assert.equal(c.nextVocalInMs, null);
});

// -------------------------------------------------- whole-track sweep

test('across the whole track no active word ever carries a countdown or a gap state', () => {
	const first = REAL_WORDS[0].start_s;
	const last = REAL_WORDS[REAL_WORDS.length - 1].end_s;
	let activeSamples = 0;
	for (let t = first; t <= last; t += 0.02) {
		const c = at(t);
		if (c.wordIndex === null) continue;
		activeSamples += 1;
		assert.equal(c.nextVocalInMs, null, `countdown while word ${c.wordIndex} active at t=${t.toFixed(2)}`);
		assert.notEqual(c.state, 'gap', `gap state while word ${c.wordIndex} active at t=${t.toFixed(2)}`);
		assert.notEqual(c.state, 'preroll');
		assert.notEqual(c.state, 'outro');
	}
	assert.equal(activeSamples > 1500, true, `only ${activeSamples} active samples; sweep did not cover the long word`);
});

test('every emitted LyricsFrame obeys the same invariant', () => {
	const first = REAL_WORDS[0].start_s;
	const last = REAL_WORDS[REAL_WORDS.length - 1].end_s;
	for (let t = first; t <= last; t += 0.05) {
		const f = frameAt(t);
		if (f.word_index === null) continue;
		assert.equal(f.next_vocal_in_ms, null, `frame carried a countdown with word_index ${f.word_index}`);
		assert.equal(f.state, 'line');
		assert.notEqual(f.word_span_ms, null, 'an active word must report its span');
		assert.equal(f.progress.word !== null, true);
	}
});

test('a countdown only ever appears with a null word_index', () => {
	const first = REAL_WORDS[0].start_s - 5;
	const last = REAL_WORDS[REAL_WORDS.length - 1].end_s + 5;
	let countdowns = 0;
	for (let t = first; t <= last; t += 0.05) {
		const f = frameAt(t);
		if (f.next_vocal_in_ms === null) continue;
		countdowns += 1;
		assert.equal(f.word_index, null, `countdown at t=${t.toFixed(2)} with an active word`);
		assert.equal(f.next_vocal_in_ms >= 0, true, 'a countdown must never run negative');
	}
	assert.equal(countdowns > 0, true, 'sweep produced no countdowns at all; fixture is wrong');
});

// ------------------------------------------- half-open, the other direction
// [start_s, end_s) is end-EXCLUSIVE and start-INCLUSIVE. A cursor that only
// inspects the word BEFORE its binary-search insertion point reports a
// one-sample gap on every exact onset. Swept rather than spot-checked, because
// the bug only shows on the exact sample.

test('every word in the fixture is active at exactly its own start_s', () => {
	for (let i = 0; i < REAL_WORDS.length; i += 1) {
		const c = at(REAL_WORDS[i].start_s);
		assert.equal(c.wordIndex, i, `word ${i} ("${REAL_WORDS[i].word}") dead on its own onset`);
		assert.equal(c.nextVocalInMs, null, `phantom countdown on the onset of word ${i}`);
	}
});

test('no word in the fixture is active at exactly its own end_s', () => {
	for (let i = 0; i < REAL_WORDS.length; i += 1) {
		const c = at(REAL_WORDS[i].end_s);
		assert.notEqual(c.wordIndex, i, `word ${i} ("${REAL_WORDS[i].word}") outlived its end_s`);
	}
});

test('the first word of every line is active at exactly the line start_s', () => {
	for (const line of track.lines) {
		const c = at(line.start_s);
		assert.equal(c.lineIndex, line.index, `line ${line.index} dead on its own start_s`);
		assert.equal(c.wordIndex, line.first_word);
	}
});

test('a line ending on the exact instant the next begins falls through neither', () => {
	// No pair in the real pred-r4c output is exactly coincident (checked: 0 of
	// 646 boundaries across all three own-crate tracks), so the touching
	// boundary is CONSTRUCTED here from the real words to exercise the code
	// path. Only the two timestamps are adjusted; nothing is invented as data.
	const touching = REAL_WORDS.map((w) => ({ ...w }));
	const seam = 156.36; // "lightning" end, line 2 -> line 3 in this fixture
	touching[14] = { ...touching[14], end_s: seam };
	touching[15] = { ...touching[15], start_s: seam };
	const t2 = build.adaptLyricTrack(META, apiTrackFrom(touching));
	const map2 = cursor.buildWordLineMap(t2);
	const c = cursor.resolveCursor(t2, map2, seam, 0);
	assert.equal(c.wordIndex, 15, 'the seam sample must belong to the STARTING word');
	assert.equal(c.lineIndex, map2[15], 'the seam sample must belong to the STARTING line');
	assert.equal(c.nextVocalInMs, null, 'a seam is not a gap');
	assert.notEqual(c.state, 'gap');
});
