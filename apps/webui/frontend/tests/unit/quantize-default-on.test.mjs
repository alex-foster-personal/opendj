import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { after, afterEach, before, beforeEach, test } from 'node:test';

import { FakeAudioContext, installWindow } from './fixtures/fake-web-audio.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * DECKUX-39 (Q1-DEFAULT-ON, the maintainer B4): Quantize "should always default to on.
 * If something turns it off, that's not a user, it should turn back on again
 * after."
 *
 * Regression lines:
 * - if a deck boots with Quantize off then broken
 * - if a load leaves Quantize off then broken
 * - if a user off (by_user: true) does not hold within the track then broken
 * - if an off without by_user lands, or its WARN does not name the caller, then broken
 * - if the next load does not re-enable Quantize after a user off then broken
 * - if a failed load ends the user's off on the track still loaded then broken
 */

const API_BASE = 'https://quantize-default-on.example.test';
const SID_A = 'a'.repeat(40);
const SID_B = 'b'.repeat(40);
const SID_MISSING = 'c'.repeat(40);
const STRETCH_STUB = fileURLToPath(new URL('./fixtures/stretch-deck-processor-load-stub.ts', import.meta.url));
const realFetch = globalThis.fetch;
const realWarn = console.warn;

let m;
let uninstall = null;
let warnings = [];

function json(body, status = 200) {
	return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

/** The daemon routes a deck load reads, for two loadable tracks and one that 404s. */
function installDaemon() {
	const bands = { length: 0, low: [], mid: [], high: [] };
	globalThis.fetch = async (input) => {
		const url = input instanceof Request ? input.url : String(input);
		if (url.includes(SID_MISSING)) {
			return json({ detail: { code: 'TRACK_NOT_FOUND', message: 'no such track' } }, 404);
		}
		if (url.includes('/anlz?')) {
			return json({
				stable_id: url.match(/[0-9a-f]{40}/)[0],
				points: 38_400,
				waveform: { kind: 'mono', preview: { ...bands }, detail: { ...bands } },
				beatgrid: { source: 'rekordbox', beat_count: 0, beats: [] },
				beatgrid_source: 'rekordbox',
				cues: [],
				phrases: [],
				vocals: { status: 'not_analyzed' },
				local_waveform: { status: 'decoded' }
			});
		}
		if (url.endsWith('/hot-cues')) {
			return json(['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'].map((slot) => ({ slot, cue: null, revision: `empty-${slot}` })));
		}
		if (url.endsWith('/audio')) return new Response(new Uint8Array(4 * 48_000 * 4));
		const track = url.match(/\/tracks\/([0-9a-f]{40})(\/stems)?$/);
		if (track?.[2] !== undefined) return json({ detail: { code: 'STEMS_NOT_FOUND', message: 'no stems' } }, 404);
		if (track !== null) return json({ stable_id: track[1], title: 'Quantize', artist: 'Fixture', bpm: null });
		throw new Error(`unexpected request ${url}`);
	};
}

const quantizeOn = (deck) => m.ipc.queryPerformanceState().decks[deck].quantize_enabled;
const dispatchViaBridge = (command) => globalThis.window.musicDjToolsPerformance.dispatch(command);
const userToggle = (deck, enabled) =>
	m.ipc.runPerformanceCommandFromUi({ type: 'quantize', deck, enabled, by_user: true });
const quantizeWarnings = () => warnings.filter((line) => line.includes('[quantize]'));

before(async () => {
	installWindow('');
	globalThis.AudioContext = FakeAudioContext;
	globalThis.requestAnimationFrame = () => 0;
	globalThis.cancelAnimationFrame = () => {};
	m = await loadTypeScriptModule('tests/unit/fixtures/quantize-default-on-entry.ts', {
		viteApiBase: API_BASE,
		alias: { '$lib/rb/stretch-adapter': STRETCH_STUB }
	});
});

beforeEach(async () => {
	await m.audio.engine.dispose();
	FakeAudioContext.instances = [];
	m.registry.resetAudioContextRegistryForTest();
	for (const toast of [...m.stores.toasts]) m.stores.dismissToast(toast.logId);
	installDaemon();
	warnings = [];
	console.warn = (...args) => {
		warnings.push(args.map(String).join(' '));
	};
	uninstall = m.ipc.installPerformanceBrowserIpc();
});

afterEach(() => {
	uninstall?.();
	uninstall = null;
	console.warn = realWarn;
	globalThis.fetch = realFetch;
});

after(async () => {
	await m.audio.engine.dispose();
	delete globalThis.AudioContext;
	delete globalThis.requestAnimationFrame;
	delete globalThis.cancelAnimationFrame;
	delete globalThis.window;
});

test('at boot every deck has Quantize on', () => {
	for (const deck of [1, 2, 3, 4]) {
		assert.equal(quantizeOn(deck), true, `if deck ${deck} boots with Quantize off then broken`);
	}
});

test('at load Quantize is on', async () => {
	await dispatchViaBridge({ type: 'load', deck: 1, stable_id: SID_A });
	assert.equal(m.ipc.queryPerformanceState().decks[1].stable_id, SID_A, 'precondition: the track loaded');
	assert.equal(quantizeOn(1), true, 'if a load leaves Quantize off then broken');
});

test('a user off holds within the track and logs no revert', async () => {
	await dispatchViaBridge({ type: 'load', deck: 1, stable_id: SID_A });
	const result = await userToggle(1, false);
	assert.deepEqual(result, { ok: true });
	assert.equal(quantizeOn(1), false, 'if a user off (by_user: true) does not land then broken');
	// Unrelated commands on the same track must not bring it back.
	await dispatchViaBridge({ type: 'quantize_grid', deck: 1, beats: 4 });
	await dispatchViaBridge({ type: 'pitch_range', deck: 1, range: 8 });
	assert.equal(quantizeOn(1), false, 'if a user off does not hold within the track then broken');
	assert.deepEqual(quantizeWarnings(), [], 'a user off is not a revert and must not WARN');
});

test('a programmatic off through the IPC bridge reverts at once and the WARN names the bridge', async () => {
	await dispatchViaBridge({ type: 'quantize', deck: 2, enabled: false });
	assert.equal(quantizeOn(2), true, 'if an off without by_user lands then broken');
	const [line, ...rest] = quantizeWarnings();
	assert.deepEqual(rest, [], 'one revert, one WARN');
	assert.match(line, /CH2: Quantize off without user provenance not applied, Quantize stays on/);
	assert.match(line, /caller: window\.musicDjToolsPerformance\.dispatch \(IPC bridge\)/);
});

test('a programmatic off through dispatchPerformanceCommand names the calling function', async () => {
	async function restoreDeckConfigLikeCaller() {
		return m.ipc.dispatchPerformanceCommand({ type: 'quantize', deck: 3, enabled: false });
	}
	await restoreDeckConfigLikeCaller();
	assert.equal(quantizeOn(3), true);
	assert.match(
		quantizeWarnings().join('\n'),
		/caller: .*restoreDeckConfigLikeCaller/,
		'if the WARN does not name the caller then a reverted off cannot be traced'
	);
});

test('an agent order off without user provenance is REFUSED naming the flag; with by_user it lands', async () => {
	const refused = await m.executeAgentOrder({ kind: 'single', payload: { type: 'quantize', deck: 4, enabled: false } });
	assert.equal(refused.steps[0].status, 'failed', 'if an agent command off is silently reverted instead of refused then broken');
	assert.match(refused.steps[0].error, /send by_user: true when a person asked for this/);
	assert.equal(quantizeOn(4), true, 'if an agent order without by_user turns Quantize off then broken');

	warnings = [];
	await m.executeAgentOrder({ kind: 'single', payload: { type: 'quantize', deck: 4, enabled: false, by_user: true } });
	assert.equal(quantizeOn(4), false, 'if an agent relaying a user ask cannot turn Quantize off then broken');
	assert.deepEqual(quantizeWarnings(), []);
});

test('the next load re-enables Quantize after a user off', async () => {
	await dispatchViaBridge({ type: 'load', deck: 1, stable_id: SID_A });
	await userToggle(1, false);
	assert.equal(quantizeOn(1), false, 'precondition: the user turned it off');
	await dispatchViaBridge({ type: 'load', deck: 1, stable_id: SID_B });
	assert.equal(m.ipc.queryPerformanceState().decks[1].stable_id, SID_B, 'precondition: the new track loaded');
	assert.equal(quantizeOn(1), true, 'if the next load does not re-enable Quantize after a user off then broken');
});

test('a failed load keeps the user off on the track still loaded', async () => {
	await dispatchViaBridge({ type: 'load', deck: 1, stable_id: SID_A });
	await userToggle(1, false);
	await assert.rejects(dispatchViaBridge({ type: 'load', deck: 1, stable_id: SID_MISSING, suppressCommandErrorToast: true }));
	assert.equal(m.ipc.queryPerformanceState().decks[1].stable_id, SID_A, 'precondition: the old track is still loaded');
	assert.equal(quantizeOn(1), false, 'if a failed load ends the user off on the track still loaded then broken');
});

test('by_user is validated like every other wire field', async () => {
	await assert.rejects(
		dispatchViaBridge({ type: 'quantize', deck: 1, enabled: false, by_user: 'yes' }),
		/by_user/
	);
	assert.equal(quantizeOn(1), true);
});

test('dispatchCallerFromStack skips the dispatcher frames', () => {
	const stack = [
		'Error',
		'    at dispatchCallerFromStack (x.js:1:1)',
		'    at dispatchPerformanceCommand (x.js:2:1)',
		'    at runPerformanceCommandFromUi (x.js:3:1)',
		'    at toggleQuantize (Deck.svelte:4:1)'
	].join('\n');
	assert.equal(m.ipc.dispatchCallerFromStack(stack), 'at toggleQuantize (Deck.svelte:4:1)');
	assert.equal(m.ipc.dispatchCallerFromStack(undefined), 'unknown caller');
});

test('an app-internal off after a user off leaves the user off alone (never flips it on)', async () => {
	await dispatchViaBridge({ type: 'load', deck: 1, stable_id: SID_A });
	await userToggle(1, false);
	await dispatchViaBridge({ type: 'quantize', deck: 1, enabled: false });
	assert.equal(quantizeOn(1), false, 'if an internal off re-enables a user off then broken');
	assert.match(quantizeWarnings().join('\n'), /CH1: .*Quantize stays off/);
});
