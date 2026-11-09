// requirement: TRYER-9999
// A try-er lanes body missing a promised curve is a failure naming the deck and curve, never an empty line.
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let check;

before(async () => {
	check = await loadTypeScriptModule('src/lib/tryer/tryer-lanes-check.ts');
});

function deck(number, curves) {
	return { deck: number, beats: [], peaks: [], playing: [[0, 10]], curves };
}

const FULL = { fader: [[0, 1]], eq_low: [[0, 0.5]], filter: [[0, 0.5]] };

test('complete lanes pass', () => {
	assert.equal(check.missingLaneCurve({ decks: [deck(1, FULL), deck(2, FULL)] }), null);
});

test('a deck missing filter fails naming the deck and the curve', () => {
	const { filter: _dropped, ...withoutFilter } = FULL;
	assert.equal(
		check.missingLaneCurve({ decks: [deck(1, FULL), deck(2, withoutFilter)] }),
		'deck 2 has no filter curve'
	);
});

test('an empty curve that is present is still a curve (no movement), not missing data', () => {
	assert.equal(check.missingLaneCurve({ decks: [deck(1, { ...FULL, eq_low: [] })] }), null);
});
