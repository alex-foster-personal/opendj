import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * parseCamelotKey used to accept numbered Camelot notation ONLY, so a track
 * tagged in standard musical notation ("Gm") rendered with no key colour and
 * no hover label, and AutoPlay rejected every candidate in such a playlist
 * because camelotKeysAreCompatible could not parse either side.
 *
 * [if] parseCamelotKey('Gm') is null [then] the key column renders uncoloured
 *   and AutoPlay strands the whole playlist - broken.
 * [if] a musical key maps to a DIFFERENT wheel position than its Camelot
 *   equivalent [then] the compatibility algebra silently lies - broken.
 * [if] an unknown spelling parses to anything but null [then] the parser is
 *   guessing, which the fail-fast contract forbids - broken.
 */

let camelot;
let color;
let autoPlay;

before(async () => {
	camelot = await loadTypeScriptModule('src/lib/player/key/camelot.ts');
	color = await loadTypeScriptModule('src/lib/rb/camelot-color.ts');
	autoPlay = await loadTypeScriptModule('src/lib/rb/auto-play.ts');
});

/** The 24 keys, each in the musical spelling a vendor writes and the Camelot
 * label it must resolve to. Hand-checked against the published Camelot wheel,
 * not generated from the table under test. */
const WHEEL = [
	['Abm', '1A'],
	['B', '1B'],
	['Ebm', '2A'],
	['F#', '2B'],
	['Bbm', '3A'],
	['Db', '3B'],
	['Fm', '4A'],
	['Ab', '4B'],
	['Cm', '5A'],
	['Eb', '5B'],
	['Gm', '6A'],
	['Bb', '6B'],
	['Dm', '7A'],
	['F', '7B'],
	['Am', '8A'],
	['C', '8B'],
	['Em', '9A'],
	['G', '9B'],
	['Bm', '10A'],
	['D', '10B'],
	['F#m', '11A'],
	['A', '11B'],
	['C#m', '12A'],
	['E', '12B']
];

describe('parseCamelotKey: numbered Camelot notation (unchanged)', () => {
	it('parses both wheels and both cases', () => {
		const { parseCamelotKey } = camelot;
		assert.deepEqual(parseCamelotKey('8A'), { number: 8, mode: 'A', root: 9 });
		assert.deepEqual(parseCamelotKey('12b'), { number: 12, mode: 'B', root: 4 });
		assert.deepEqual(parseCamelotKey(' 1A '), { number: 1, mode: 'A', root: 8 });
	});

	it('still rejects out-of-range numbers and unknown wheels', () => {
		const { parseCamelotKey } = camelot;
		assert.equal(parseCamelotKey('13A'), null);
		assert.equal(parseCamelotKey('0A'), null);
		assert.equal(parseCamelotKey('8C'), null);
		assert.equal(parseCamelotKey(null), null);
		assert.equal(parseCamelotKey(''), null);
	});
});

describe('parseCamelotKey: standard musical notation', () => {
	it('maps all 24 keys onto the published Camelot wheel', () => {
		const { parseCamelotKey } = camelot;
		for (const [musical, expected] of WHEEL) {
			const parsed = parseCamelotKey(musical);
			assert.notEqual(parsed, null, `${musical} must parse`);
			assert.equal(`${parsed.number}${parsed.mode}`, expected, `${musical} -> ${expected}`);
		}
	});

	it('agrees exactly with the Camelot label for the same key', () => {
		const { parseCamelotKey } = camelot;
		for (const [musical, expected] of WHEEL) {
			assert.deepEqual(parseCamelotKey(musical), parseCamelotKey(expected), musical);
		}
	});

	it('accepts the spelled-out and one-letter mode forms', () => {
		const { parseCamelotKey } = camelot;
		const gMinor = parseCamelotKey('6A');
		for (const spelling of ['Gm', 'Gmin', 'Gminor', 'G minor', 'G Minor', 'G MIN']) {
			assert.deepEqual(parseCamelotKey(spelling), gMinor, spelling);
		}
		const aFlatMajor = parseCamelotKey('4B');
		for (const spelling of ['Ab', 'AbM', 'Abmaj', 'Ab major', 'Ab Major', 'A\u266d major']) {
			assert.deepEqual(parseCamelotKey(spelling), aFlatMajor, spelling);
		}
	});

	it('treats a bare note as major and a bare m as minor', () => {
		const { parseCamelotKey } = camelot;
		assert.equal(parseCamelotKey('C').mode, 'B');
		assert.equal(parseCamelotKey('Cm').mode, 'A');
		// Case is significant for the one-letter forms only.
		assert.equal(parseCamelotKey('CM').mode, 'B');
	});

	it('accepts ASCII and Unicode accidentals identically', () => {
		const { parseCamelotKey } = camelot;
		assert.deepEqual(parseCamelotKey('F#m'), parseCamelotKey('F\u266fm'));
		assert.deepEqual(parseCamelotKey('Bbm'), parseCamelotKey('B\u266dm'));
		// Enharmonics land on the same wheel position, as they must.
		assert.deepEqual(parseCamelotKey('C#'), parseCamelotKey('Db'));
	});

	it('rejects unknown spellings instead of guessing', () => {
		const { parseCamelotKey } = camelot;
		for (const bad of [
			'H',
			'Gmm',
			'G#b',
			'G-min',
			'G dorian',
			'Gmajorish',
			'unknown',
			'8A minor'
		]) {
			assert.equal(parseCamelotKey(bad), null, `${bad} must not parse`);
		}
	});
});

describe('musical notation flows into the key algebra', () => {
	it('camelotKeysAreCompatible mixes notations on either side', () => {
		const { camelotKeysAreCompatible } = camelot;
		// Gm = 6A. Same wheel number, one step either way, and cross-wheel.
		assert.equal(camelotKeysAreCompatible('Gm', '6A'), true);
		assert.equal(camelotKeysAreCompatible('Gm', 'Dm'), true); // 6A/7A
		assert.equal(camelotKeysAreCompatible('Gm', 'Cm'), true); // 6A/5A
		assert.equal(camelotKeysAreCompatible('Gm', 'Bb'), true); // 6A/6B
		assert.equal(camelotKeysAreCompatible('Gm', '9A'), false); // 6A/9A
		assert.equal(camelotKeysAreCompatible('Gm', 'not-a-key'), false);
	});

	it('effectiveCamelotKey normalizes a musical label to its Camelot form', () => {
		const { effectiveCamelotKey } = camelot;
		assert.equal(effectiveCamelotKey('Gm', 0), '6A');
		assert.equal(effectiveCamelotKey('Gm', 1), '1A'); // G minor +1 = Ab minor
		// Unparseable metadata is still passed through untouched, never invented.
		assert.equal(effectiveCamelotKey('unknown', 0), 'unknown');
	});

	it('deriveKeySyncNudge accepts musical notation on both decks', () => {
		const { deriveKeySyncNudge } = camelot;
		assert.equal(deriveKeySyncNudge('Gm', '6A', 0, 0, 0), 0);
		assert.equal(deriveKeySyncNudge('Am', 'Bm', 0, 0, 0), 2); // 8A -> 10A
	});
});

describe('musical notation renders in the browser table', () => {
	it('colours a musical key the same as its Camelot equivalent', () => {
		const { camelotKeyColor } = color;
		for (const [musical, expected] of WHEEL) {
			const swatch = camelotKeyColor(musical);
			assert.notEqual(swatch, null, `${musical} must colour`);
			assert.equal(swatch, camelotKeyColor(expected), musical);
		}
	});

	it('hover label normalizes musical notation to Camelot', () => {
		const { camelotKeyHoverLabel } = color;
		assert.equal(camelotKeyHoverLabel('Gm'), '6A \u2192 G minor');
		assert.equal(camelotKeyHoverLabel('6A'), '6A \u2192 G minor');
		assert.equal(camelotKeyHoverLabel('Ab major'), '4B \u2192 Ab major');
		assert.equal(camelotKeyHoverLabel('10B'), '10B \u2192 D major');
	});

	it('renders nothing for a key in no notation, rather than a wrong swatch', () => {
		const { camelotKeyColor, camelotKeyHoverLabel } = color;
		assert.equal(camelotKeyColor(null), null);
		assert.equal(camelotKeyColor('H'), null);
		assert.equal(camelotKeyHoverLabel('13A'), null);
	});
});

describe('AutoPlay candidate path accepts musical notation', () => {
	const row = (stable_id, key, bpm) => ({ stable_id, key, bpm, file_exists: true });

	it('picks a compatible follower when the playlist is tagged musically', () => {
		const { pickNextStableId } = autoPlay;
		// Gm = 6A. Dm = 7A (compatible), C#m = 12A (not), Bb = 6B (compatible).
		const playlist = [
			row('cur', 'Gm', 124),
			row('far', 'C#m', 124),
			row('near', 'Dm', 126),
			row('cross', 'Bb', 125)
		];
		const picked = pickNextStableId({
			playlist,
			current_stable_id: 'cur',
			current_key: 'Gm',
			current_bpm: 124,
			exclude_ids: new Set(),
			played_ids: new Set(),
			enforce_play_order: false,
			min_tempo_ratio: 0.84,
			max_tempo_ratio: 1.16
		});
		// Earliest compatible membership row wins; 'far' is key-incompatible.
		assert.equal(picked, 'near');
	});

	it('mixed Camelot and musical tagging resolves as one key space', () => {
		const { pickNextStableId } = autoPlay;
		const picked = pickNextStableId({
			playlist: [row('cur', '6A', 124), row('musical', 'Dm', 126)],
			current_stable_id: 'cur',
			current_key: '6A',
			current_bpm: 124,
			exclude_ids: new Set(),
			played_ids: new Set(),
			enforce_play_order: false,
			min_tempo_ratio: 0.84,
			max_tempo_ratio: 1.16
		});
		assert.equal(picked, 'musical');
	});

	it('a musically tagged playlist is no longer stranded end to end', () => {
		const { simulateAutoPlayChain } = autoPlay;
		const chain = simulateAutoPlayChain({
			playlist: [
				row('a', 'Gm', 124),
				row('b', 'Dm', 125),
				row('c', 'Am', 126)
			],
			start_stable_id: 'a',
			enforce_play_order: false,
			maximize_reach: false,
			min_tempo_ratio: 0.84,
			max_tempo_ratio: 1.16
		});
		// 6A -> 7A -> 8A: every track reachable, where before the fix the chain
		// stopped dead at the anchor.
		assert.deepEqual([...chain], ['a', 'b', 'c']);
	});
});
