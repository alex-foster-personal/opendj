// requirement: DECKGAP-7691h
// Reset now: the reset panel applies the ticked resets to THIS deck at once,
// through the typed `reset_deck_now` command agents send too, reusing the
// on-load reset (applyTrackLoadChannelReset) rather than a second copy of it.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let ipc, app, reset, reactive;
before(async () => {
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
	app = await loadTypeScriptModule('tests/unit/fixtures/pairing-snapshot-entry.ts', {
		viteApiBase: 'https://pairing.example.test'
	});
	reset = await loadTypeScriptModule('src/lib/rb/track-load-reset.ts');
	reactive = await loadTypeScriptModule('src/lib/rb/track-load-reset.svelte.ts');
});

const NONE = { level: false, stems: false, eq: false, trim: false, filter: false };
const unity = () => ({ muted: false, solo: false, gain: 0.5 });

test('reset_deck_now parses a deck and optional distinct parts, and holds no deck scope itself', () => {
	assert.deepEqual(ipc.parsePerformanceCommandForTest({ type: 'reset_deck_now', deck: 2 }), { type: 'reset_deck_now', deck: 2 });
	assert.deepEqual(
		ipc.parsePerformanceCommandForTest({ type: 'reset_deck_now', deck: 1, parts: ['eq', 'stems'] }),
		{ type: 'reset_deck_now', deck: 1, parts: ['eq', 'stems'] }
	);
	assert.equal(ipc.performanceCommandQueueScopes({ type: 'reset_deck_now', deck: 2 }), null);
	// Controls: no deck, an empty list, an unknown item, a repeat or an extra key is refused, not guessed.
	assert.throws(() => ipc.parsePerformanceCommandForTest({ type: 'reset_deck_now' }));
	assert.throws(() => ipc.parsePerformanceCommandForTest({ type: 'reset_deck_now', deck: 2, parts: [] }), /non-empty/);
	assert.throws(() => ipc.parsePerformanceCommandForTest({ type: 'reset_deck_now', deck: 2, parts: ['pitch'] }), /unknown item pitch/);
	assert.throws(() => ipc.parsePerformanceCommandForTest({ type: 'reset_deck_now', deck: 2, parts: ['eq', 'eq'] }), /named twice/);
	assert.throws(() => ipc.parsePerformanceCommandForTest({ type: 'reset_deck_now', deck: 2, level: true }));
});

test('parts override the ticked choice; omitted, the press takes the ticked ones', () => {
	reset.writeTrackLoadReset({ ...NONE, trim: true });
	assert.deepEqual(reset.resetNowPrefs(undefined), { ...NONE, trim: true });
	assert.deepEqual(reset.resetNowPrefs(['level', 'stems']), { ...NONE, level: true, stems: true });
});

test('stem reset writes only offered, non-neutral controls, and nothing when stems is off', () => {
	const controls = { vocal: { muted: true, solo: false, gain: 0.5 }, instrumental: unity(), drums: { muted: false, solo: true, gain: 0.8 }, bass: { muted: true, solo: false, gain: 0.5 }, other: unity() };
	assert.deepEqual(
		reset.stemResetActions({ ...NONE, stems: true }, controls, ['vocal', 'instrumental', 'drums']),
		[
			{ kind: 'stem_mute', stem: 'vocal', muted: false },
			{ kind: 'stem_solo', stem: 'drums', solo: false },
			{ kind: 'stem_gain', stem: 'drums', value: 0.5 }
		],
		'bass is not offered by this bundle, so it is left alone'
	);
	assert.deepEqual(reset.stemResetActions(NONE, controls, ['vocal', 'drums']), []);
});

test('dispatching reset_deck_now resets only the named items on that deck, now', async () => {
	const hadWindow = 'window' in globalThis;
	if (!hadWindow) globalThis.window = {};
	const hadLocation = 'location' in globalThis;
	if (!hadLocation) globalThis.location = { search: '' }; // the Web Audio engine reads it on a fader write
	const uninstall = app.installPerformanceBrowserIpc();
	try {
		await app.dispatchPerformanceCommand({ type: 'track_load_reset', ...NONE });
		const ch = app.mixerState.channels[2];
		Object.assign(ch, { fader: 0.2, trim: 0.9, eq_high: 0.5, eq_mid: 0.1, eq_low: 0.7, filter: 0.3 });
		const other = app.mixerState.channels[1];
		Object.assign(other, { fader: 0.2, eq_mid: 0.1 });
		await app.dispatchPerformanceCommand({ type: 'reset_deck_now', deck: 2, parts: ['level', 'eq'] });
		assert.equal(ch.fader, 1, 'level back to full');
		assert.deepEqual([ch.eq_high, ch.eq_mid, ch.eq_low], [0.5, 0.5, 0.5], 'EQ flat');
		assert.equal(ch.trim, 0.9, 'trim not named, so kept');
		assert.equal(ch.filter, 0.3, 'filter not named, so kept');
		assert.deepEqual([other.fader, other.eq_mid], [0.2, 0.1], 'another deck is untouched');
		// Omitted parts: the ticked choice, here trim only.
		await app.dispatchPerformanceCommand({ type: 'track_load_reset', ...NONE, trim: true });
		await app.dispatchPerformanceCommand({ type: 'reset_deck_now', deck: 2 });
		assert.equal(ch.trim, 0.5);
		assert.equal(ch.filter, 0.3);
	} finally {
		uninstall();
		if (!hadWindow) delete globalThis.window;
		if (!hadLocation) delete globalThis.location;
	}
});

test('the panel button dispatches reset_deck_now for the deck that opened it, styled as a secondary control', async () => {
	assert.deepEqual(reactive.resetDeckNowCommand(3), { type: 'reset_deck_now', deck: 3 });
	const panel = await readFile('src/lib/components/rb/TrackLoadResetPanel.svelte', 'utf8');
	assert.match(panel, /data-testid="track-load-reset-now"/);
	assert.match(panel, /runPerformanceCommandFromUi\(resetDeckNowCommand\(deck\)\)/);
	const now = panel.slice(panel.indexOf('\t.now {'), panel.indexOf('\t.now:hover'));
	assert.match(now, /border: 1px solid/);
	assert.match(now, /background: transparent/);
	assert.doesNotMatch(now, /--rb-accent/, 'never the accent colour');
	// StemRow hands the click to its host (onOpenResetConfig); both hosts name the deck.
	for (const host of ['src/lib/components/rb/Deck.svelte', 'src/lib/components/rb/mixer/ChannelStrip.svelte']) {
		const src = await readFile(host, 'utf8');
		assert.match(src, /toggleTrackLoadResetPanel\('stems', el, deck\.deck_id\)/, host);
	}
	// The channel half is the on-load reset itself, not a second copy.
	const src = await readFile('src/lib/rb/performance-ipc.svelte.ts', 'utf8');
	const body = src.slice(src.indexOf('async function _resetDeckNow('), src.indexOf('function _commandDeck('));
	assert.match(body, /applyTrackLoadChannelReset\(mixerState\.channels\[deck\]/);
});
