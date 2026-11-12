// requirement: PADFX-11718
// [if] pad_mode is missing from query, dispatch, or the UI mirror [then] broken
// [if] PadModeMenu still calls selectPadMode directly [then] broken
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let ipc;
let PAD_MODE_IDS;
let BEAT_FX_UNIT_NOT_WIRED_REASON;

before(async () => {
	ipc = await loadTypeScriptModule('tests/unit/fixtures/pad-mode-ipc-entry.ts');
	const catalog = await loadTypeScriptModule('src/lib/rb/pad-mode-catalog.ts');
	PAD_MODE_IDS = catalog.PAD_MODE_IDS;
	BEAT_FX_UNIT_NOT_WIRED_REASON = ipc.BEAT_FX_UNIT_NOT_WIRED_REASON;
});

test('query publishes pad_mode per deck and structuredClone stays serializable', () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const handle = globalThis.window.musicDjToolsPerformance;
		const snap = handle.query();
		for (const deck of [1, 2, 3, 4]) {
			assert.equal(snap.decks[deck].pad_mode.selected, 'hot-cue');
			assert.deepEqual(
				snap.decks[deck].pad_mode.modes.map((m) => m.id),
				[...PAD_MODE_IDS]
			);
		}
		const padFx1 = snap.decks[1].pad_mode.modes.find((m) => m.id === 'pad-fx-1');
		const padFx2 = snap.decks[1].pad_mode.modes.find((m) => m.id === 'pad-fx-2');
		const hotCue = snap.decks[1].pad_mode.modes.find((m) => m.id === 'hot-cue');
		const beatLoop = snap.decks[1].pad_mode.modes.find((m) => m.id === 'beat-loop');
		assert.equal(padFx1.enabled, false);
		assert.equal(padFx1.reason, BEAT_FX_UNIT_NOT_WIRED_REASON);
		assert.equal(padFx2.enabled, false);
		assert.equal(padFx2.reason, BEAT_FX_UNIT_NOT_WIRED_REASON);
		assert.equal(hotCue.enabled, true);
		assert.equal(hotCue.reason, null);
		assert.equal(beatLoop.enabled, false);
		assert.ok(beatLoop.reason?.length);
		structuredClone(snap);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('pad_mode has null queue scope and refuses unavailable modes without changing selection', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const handle = globalThis.window.musicDjToolsPerformance;
		assert.equal(
			ipc.performanceCommandQueueScopes({ type: 'pad_mode', deck: 2, mode: 'pad-fx-1' }),
			null
		);
		await assert.rejects(
			() => handle.dispatch({ type: 'pad_mode', deck: 2, mode: 'pad-fx-1' }),
			/Pad FX needs the beat FX unit/
		);
		assert.equal(handle.query().decks[2].pad_mode.selected, 'hot-cue');
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('pad_mode selects when the unit allows it and reflects per-deck refusal reasons', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	const uninstallPort = ipc.installBeatFxUnitPort({
		availability: (deck) =>
			deck === '3' ? { enabled: false, reason: 'beat FX unit flag off' } : { enabled: true },
		send: () => {}
	});
	try {
		const handle = globalThis.window.musicDjToolsPerformance;
		const deck3PadFx1 = handle.query().decks[3].pad_mode.modes.find((m) => m.id === 'pad-fx-1');
		assert.equal(deck3PadFx1.reason, 'beat FX unit flag off');
		await assert.rejects(
			() => handle.dispatch({ type: 'pad_mode', deck: 3, mode: 'pad-fx-1' }),
			/beat FX unit flag off/
		);
		const after = await handle.dispatch({ type: 'pad_mode', deck: 2, mode: 'pad-fx-2' });
		assert.equal(after.decks[2].pad_mode.selected, 'pad-fx-2');
		for (const deck of [1, 3, 4]) {
			assert.equal(after.decks[deck].pad_mode.selected, 'hot-cue');
		}
		await handle.dispatch({ type: 'pad_mode', deck: 2, mode: 'hot-cue' });
		assert.equal(handle.query().decks[2].pad_mode.selected, 'hot-cue');
	} finally {
		uninstallPort();
		uninstall();
		delete globalThis.window;
	}
});

test('the parser refuses illegal pad_mode commands and unbuilt modes at execution', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const handle = globalThis.window.musicDjToolsPerformance;
		for (const bad of [
			{ type: 'pad_mode', deck: 2, mode: 'nope' },
			{ type: 'pad_mode', deck: 2, mode: 1 },
			{ type: 'pad_mode', deck: 2 },
			{ type: 'pad_mode', deck: 2, mode: 'pad-fx-1', extra: 1 },
			{ type: 'pad_mode', deck: 9, mode: 'hot-cue' }
		]) {
			await assert.rejects(() => handle.dispatch(bad));
		}
		await assert.rejects(
			() => handle.dispatch({ type: 'pad_mode', deck: 2, mode: 'beat-loop' }),
			/Beat Loop/
		);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('source wiring: menus dispatch pad_mode and the UI mirror publishes it', () => {
	// Whitespace-tolerant: prettier wraps the call in the deeper-indented React twin.
	const menuRe =
		/runPerformanceCommandFromUi\(\{\s*type: 'pad_mode',\s*deck: deckId,\s*mode: entry\.id\s*\}\)/;
	for (const twin of ['PadModeMenu.svelte', 'PadModeMenu.tsx']) {
		const menu = readFileSync(`src/lib/components/rb/deck/${twin}`, 'utf8');
		assert.match(menu, menuRe, twin);
		assert.doesNotMatch(menu, /selectPadMode\(/, twin);
	}
	const mirror = readFileSync('src/lib/rb/ui-mirror.ts', 'utf8');
	assert.match(mirror, /pad_mode: deck\.pad_mode/);
});
