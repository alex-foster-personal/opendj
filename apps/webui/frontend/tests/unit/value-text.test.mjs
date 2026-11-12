// requirement: A11Y-11269
// [if] a mixer slider value is formatted for screen readers [then] valueText returns human units, [else stop]
// [if] identical announcements fire within 500 ms [then] announce dedupes them, [else stop]
// [if] the same deck is selected again [then] noteRecentDeck is silent, [else stop]
import assert from 'node:assert/strict';
import { before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let valueTextMod;
let constantsMod;
let recentDeckMod;
/** @type {import('../../src/lib/a11y/value-text.ts').valueText} */
let valueText;
/** @type {import('../../src/lib/a11y/value-text.ts').knobValueKind} */
let knobValueKind;
let announce;
let announcer;
let ANNOUNCE_DEDUPE_MS;
let noteRecentDeck;

before(async () => {
	valueTextMod = await loadTypeScriptModule('src/lib/a11y/value-text.ts');
	constantsMod = await loadTypeScriptModule('src/lib/player/constants.ts');
	recentDeckMod = await loadTypeScriptModule(
		'tests/unit/fixtures/a11y-announce-recent-deck-entry.ts'
	);
	valueText = valueTextMod.valueText;
	knobValueKind = valueTextMod.knobValueKind;
	announce = recentDeckMod.announce;
	announcer = recentDeckMod.announcer;
	ANNOUNCE_DEDUPE_MS = recentDeckMod.ANNOUNCE_DEDUPE_MS;
	noteRecentDeck = recentDeckMod.noteRecentDeck;
});

beforeEach(() => {
	recentDeckMod._resetAnnouncerForTests();
	recentDeckMod._resetRecentDeckForTests();
});

test('A11Y-11269 eq valueText at ends, centre and interior', () => {
	assert.equal(valueText('eq', 0), '-26 dB');
	assert.equal(valueText('eq', 0.5), '0 dB');
	assert.equal(valueText('eq', 1), `+${constantsMod.EQ_MAX_DB} dB`);
	assert.equal(valueText('eq', 0.75), '+3 dB');
	assert.equal(valueText('eq', 0.25), '-13 dB');
	assert.equal(valueText('eq', 0.6), '+1.2 dB');
});

test('A11Y-11269 trim valueText at ends, centre and interior', () => {
	assert.equal(valueText('trim', 0), '-∞ dB');
	assert.equal(valueText('trim', 0.5), '0 dB');
	assert.equal(valueText('trim', 1), '+6 dB');
	assert.equal(valueText('trim', 0.25), '-6 dB');
});

test('A11Y-11269 filter valueText', () => {
	assert.equal(valueText('filter', 0), 'low-pass 100%');
	assert.equal(valueText('filter', 0.3), 'low-pass 40%');
	assert.equal(valueText('filter', 0.5), 'off');
	assert.equal(valueText('filter', 0.505), 'off');
	assert.equal(valueText('filter', 0.7), 'high-pass 40%');
	assert.equal(valueText('filter', 1), 'high-pass 100%');
});

test('A11Y-11269 bipolar valueText', () => {
	assert.equal(valueText('bipolar', 0.5), 'off');
	assert.equal(valueText('bipolar', 0.3), '40% counter-clockwise');
	assert.equal(valueText('bipolar', 1), '100% clockwise');
});

test('A11Y-11269 fader and crossfader valueText', () => {
	assert.equal(valueText('fader', 0), '0%');
	assert.equal(valueText('fader', 0.5), '50%');
	assert.equal(valueText('fader', 0.75), '75%');
	assert.equal(valueText('fader', 1), '100%');
	assert.equal(valueText('crossfader', 0), '100% left');
	assert.equal(valueText('crossfader', 0.2), '60% left');
	assert.equal(valueText('crossfader', 0.5), 'centre');
	assert.equal(valueText('crossfader', 0.8), '60% right');
	assert.equal(valueText('crossfader', 1), '100% right');
});

test('A11Y-11269 pitch valueText', () => {
	assert.equal(valueText('pitch', -16), '-16.00%');
	assert.equal(valueText('pitch', 0), '0.00%');
	assert.equal(valueText('pitch', -0.001), '0.00%');
	assert.equal(valueText('pitch', 1.25), '+1.25%');
	assert.equal(valueText('pitch', 16), '+16.00%');
});

test('A11Y-11269 knobValueKind maps roles', () => {
	assert.equal(knobValueKind('1:mid'), 'eq');
	assert.equal(knobValueKind('2:trim'), 'trim');
	assert.equal(knobValueKind('3:filter'), 'filter');
	assert.equal(knobValueKind('hp:hp-mix'), 'percent');
	assert.equal(knobValueKind('1:stem-vocal'), 'percent');
});

test('A11Y-11269 valueText rejects non-unit and non-finite inputs', () => {
	assert.throws(() => valueText('fader', Number.NaN), RangeError);
	assert.throws(() => valueText('fader', 1.5), RangeError);
});

test('A11Y-11269 announce without a node does not throw', () => {
	assert.doesNotThrow(() => announce('Deck 1 selected'));
});

test('A11Y-11269 announce dedupes within ANNOUNCE_DEDUPE_MS', async () => {
	const fake = { textContent: '', dataset: {} };
	const prevRaf = globalThis.requestAnimationFrame;
	globalThis.requestAnimationFrame = undefined;
	const tick = () => new Promise((resolve) => setTimeout(resolve, 5));
	try {
		announcer(fake);
		announce('Deck 2 selected');
		await tick();
		const afterFirst = Number.parseInt(fake.dataset.announceCount ?? '0', 10);
		announce('Deck 2 selected');
		await tick();
		const afterDup = Number.parseInt(fake.dataset.announceCount ?? '0', 10);
		assert.equal(afterDup, afterFirst, 'duplicate text within dedupe window must not increment');
		announce('Deck 3 selected');
		await tick();
		const afterDifferent = Number.parseInt(fake.dataset.announceCount ?? '0', 10);
		assert.ok(afterDifferent > afterFirst, 'different text must announce');
		assert.equal(ANNOUNCE_DEDUPE_MS, 500);
	} finally {
		globalThis.requestAnimationFrame = prevRaf;
		recentDeckMod._resetAnnouncerForTests();
	}
});

test('A11Y-11269 second announcer mount throws', () => {
	const a = { textContent: '', dataset: {} };
	const b = { textContent: '', dataset: {} };
	announcer(a);
	assert.throws(() => announcer(b), /already mounted/);
	recentDeckMod._resetAnnouncerForTests();
});

test('A11Y-11269 noteRecentDeck announces deck changes only', async () => {
	const fake = { textContent: '', dataset: {} };
	const prevRaf = globalThis.requestAnimationFrame;
	globalThis.requestAnimationFrame = undefined;
	const tick = () => new Promise((resolve) => setTimeout(resolve, 5));
	try {
		announcer(fake);
		noteRecentDeck(2);
		await tick();
		assert.equal(fake.textContent, 'Deck 2 selected');
		const count2 = Number.parseInt(fake.dataset.announceCount ?? '0', 10);
		noteRecentDeck(3);
		await tick();
		assert.equal(fake.textContent, 'Deck 3 selected');
		const count3 = Number.parseInt(fake.dataset.announceCount ?? '0', 10);
		assert.ok(count3 > count2);
		noteRecentDeck(3);
		await tick();
		const countDup = Number.parseInt(fake.dataset.announceCount ?? '0', 10);
		assert.equal(countDup, count3, 're-selecting the same deck must not announce again');
	} finally {
		globalThis.requestAnimationFrame = prevRaf;
		recentDeckMod._resetAnnouncerForTests();
		recentDeckMod._resetRecentDeckForTests();
	}
});
