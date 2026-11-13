// requirement: PADFX-11718
// [if] a default bank does not have 8 presets with the JSON labels in order [then] broken
// [if] a malformed preset file loads without naming the offending entry [then] broken
// [if] an unregistered effect (trans) is live, or echo or a registered roll is not [then] broken
// [if] pointer down/up does not send beat_fx enabled true then enabled false for the deck [then] broken
// [if] a second held pad does not replace the first (last pad wins) [then] broken
// [if] a Release FX press does not turn the other held pads off, or hold/toggle is ignored [then] broken
// [if] arrows while held do not step beats through the division list and restore on release [then] broken
// [if] a deck whose unit is unavailable can select Pad FX, or loses the refusal reason [then] broken
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { resolve } from 'node:path';
import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const PAD_FX_BANK_SSR = [
	"export { default as PadFxBank } from '$lib/components/rb/deck/PadFxBank.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');
const src = (p) => readFileSync(resolve(FRONTEND_ROOT, p), 'utf8');
const RAW = JSON.parse(src('src/lib/rb/pad-fx-presets.json'));

let vite, presets, mode, port, state, catalog, beatFx;

test.before(async () => {
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true, watch: null },
		plugins: [svelte()],
		resolve: { alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') }, conditions: ['browser'] }
	});
	presets = await vite.ssrLoadModule('/src/lib/rb/pad-fx-presets.ts');
	mode = await vite.ssrLoadModule('/src/lib/rb/pad-fx-mode.ts');
	port = await vite.ssrLoadModule('/src/lib/rb/beat-fx-unit-port.svelte.ts');
	state = await vite.ssrLoadModule('/src/lib/rb/pad-mode-state.svelte.ts');
	catalog = await vite.ssrLoadModule('/src/lib/rb/pad-mode-catalog.ts');
	beatFx = await vite.ssrLoadModule('/src/lib/rb/beat-fx.ts');
});

test.after(async () => {
	await vite.close();
});

const clone = () => structuredClone(RAW);
const on = (effect, beats) => ({ type: 'beat_fx', target: '2', effect, beats, enabled: true });
const OFF = { type: 'beat_fx', target: '2', enabled: false };
const REL_OFF = { type: 'beat_fx_release', held: false };
const ctl = (bankId) => mode.createPadFxController(presets.padFxBank(bankId), '2');

test('default file: both banks have 8 presets with the JSON labels in order', () => {
	for (const id of presets.PAD_FX_MODE_IDS) {
		const bank = presets.padFxBank(id);
		assert.equal(bank.length, 8);
		assert.deepEqual(
			bank.map((p) => p.label),
			RAW.banks[id].map((p) => p.label)
		);
	}
});

test('malformed files throw naming the entry', () => {
	const cases = [
		[(f) => f.banks['pad-fx-1'].pop(), /banks\.pad-fx-1 must be an array of exactly 8.*got 7/],
		[(f) => (f.banks['pad-fx-1'][0].beats = 0.3), /banks\.pad-fx-1\[0\]\.beats.*0\.3/],
		[
			(f) => (f.banks['pad-fx-1'][0].amount = 5),
			/banks\.pad-fx-1\[0\] must have exactly one of beats or amount/
		],
		[(f) => (f.banks['pad-fx-2'][3].gain = 1), /banks\.pad-fx-2\[3\]\.gain is an unknown field/],
		[(f) => (f.banks['pad-fx-1'][2].effect = 'flangr'), /banks\.pad-fx-1\[2\]\.effect.*flangr/],
		[(f) => (f.version = 2), /pad-fx-presets\.version must be 1; got 2/],
		[(f) => (f.banks['pad-fx-1'][5].label = ''), /banks\.pad-fx-1\[5\]\.label/]
	];
	for (const [mutate, message] of cases) {
		const f = clone();
		mutate(f);
		assert.throws(() => presets.parsePadFxPresetFile(f), message);
	}
});

test('readiness: echo and release echo live, unregistered effects and amount presets inert with reasons', () => {
	const bank = presets.padFxBank('pad-fx-1');
	assert.deepEqual(presets.padFxPresetReadiness(bank[0]), { live: true });
	// BEATFX-14398 registered roll, so its preset comes live; trans is still unregistered.
	assert.ok(beatFx.BEAT_FX_EFFECTS.includes('roll'));
	assert.deepEqual(presets.padFxPresetReadiness(bank[2]), { live: true });
	assert.equal(bank[4].effect, 'trans');
	assert.ok(!beatFx.BEAT_FX_EFFECTS.includes('trans'));
	assert.deepEqual(presets.padFxPresetReadiness(bank[4]), {
		live: false,
		reason: 'TRANS 1/8: effect trans is not in the beat FX registry yet'
	});
	assert.deepEqual(presets.padFxPresetReadiness(bank[6]), {
		live: false,
		reason: 'REVERB 50: beat FX has no amount parameter yet'
	});
	assert.deepEqual(presets.padFxPresetReadiness(bank[7]), { live: true });
});

test('hold: press engages the deck, release turns it off', () => {
	const c = ctl('pad-fx-1');
	assert.deepEqual(c.press(0), [on('echo', 0.5)]);
	assert.deepEqual(c.release(0), [OFF]);
});

test('last pad wins: a second held pad replaces the first', () => {
	const c = ctl('pad-fx-1');
	const seq = [c.press(0), c.press(1), c.release(1), c.release(0)];
	assert.deepEqual(seq, [[on('echo', 0.5)], [on('echo', 0.25)], [OFF], []]);
});

test('Release FX hold turns the held pad off and holds release while down', () => {
	const c = ctl('pad-fx-1');
	const seq = [c.press(0), c.press(7), c.release(7), c.release(0)];
	assert.deepEqual(seq, [
		[on('echo', 0.5)],
		[OFF, { type: 'beat_fx_release', held: true, beats: 2, kind: 'echo' }],
		[REL_OFF],
		[]
	]);
});

test('Release FX toggle latches on one press and unlatches on the next', () => {
	const c = ctl('pad-fx-2');
	assert.deepEqual(c.press(7), [
		OFF,
		{ type: 'beat_fx_release', held: true, beats: 1, kind: 'echo' }
	]);
	assert.deepEqual(c.release(7), []);
	assert.equal(c.state().releaseLatched, true);
	assert.deepEqual(c.press(7), [OFF, REL_OFF]);
	assert.deepEqual(c.release(7), []);
	assert.equal(c.state().releaseLatched, false);
});

test('arrows step beats through the division list, clamp, and restore on release', () => {
	const c = ctl('pad-fx-1');
	assert.deepEqual(c.step(1), [], 'nothing held');
	c.press(0);
	const step = (beats) => [{ type: 'beat_fx', target: '2', beats }];
	assert.deepEqual(c.step(1), step(0.75));
	assert.deepEqual(c.step(-1), step(0.5));
	assert.deepEqual(c.step(-1), step(0.25));
	assert.deepEqual(c.step(-1), step(0.125));
	assert.deepEqual(c.step(-1), step(0.0625));
	assert.equal(beatFx.BEAT_FX_BEATS[0], 0.0625);
	assert.deepEqual(c.step(-1), [], 'clamped at the first division');
	c.release(0);
	assert.deepEqual(c.press(0), [on('echo', 0.5)], 'release restores the JSON beats');
});

test('controller refuses inert, double, unheld and out-of-range pads', () => {
	const c = ctl('pad-fx-1');
	assert.deepEqual(ctl('pad-fx-1').press(2), [on('roll', 0.25)], 'registered roll engages');
	assert.throws(() => c.press(4), /TRANS 1\/8: effect trans is not in the beat FX registry yet/);
	c.press(0);
	assert.throws(() => c.press(0), /already held/);
	assert.throws(() => c.release(3), /not held/);
	assert.throws(() => c.press(8), /outside 0\.\.7/);
});

test('releaseAll lets go of held pads and unlatches a toggled release', () => {
	const held = ctl('pad-fx-2');
	held.press(0);
	assert.deepEqual(held.releaseAll(), [OFF]);
	const latched = ctl('pad-fx-2');
	latched.press(7);
	latched.release(7);
	assert.deepEqual(latched.releaseAll(), [REL_OFF]);
	assert.deepEqual(latched.state(), {
		held: [],
		active: null,
		releaseLatched: false,
		beatsOverride: null
	});
});

test('unit port: unwired refuses, a flag-off port gives its reason, an enabled one selects', () => {
	assert.deepEqual(port.beatFxUnitAvailability('1'), {
		enabled: false,
		reason: port.BEAT_FX_UNIT_NOT_WIRED_REASON
	});
	assert.throws(
		() => port.sendBeatFxUnitCommand('1', REL_OFF),
		new RegExp(port.BEAT_FX_UNIT_NOT_WIRED_REASON.replace(/[()#]/g, '\\$&'))
	);
	const entry = catalog.PAD_MODE_CATALOG.find((e) => e.id === 'pad-fx-1');
	let enabled = false;
	const sent = [];
	const uninstall = port.installBeatFxUnitPort({
		availability: () =>
			enabled ? { enabled: true } : { enabled: false, reason: 'beat FX unit flag off' },
		send: (c) => sent.push(c)
	});
	try {
		assert.throws(
			() => port.installBeatFxUnitPort({ availability: () => ({ enabled: true }), send: () => {} }),
			/already installed/
		);
		assert.deepEqual(state.padModeSelectable(3, entry), {
			enabled: false,
			reason: 'beat FX unit flag off'
		});
		assert.throws(() => state.selectPadMode(3, 'pad-fx-1'), /beat FX unit flag off/);
		assert.equal(state.padModeFor(3), 'hot-cue');
		enabled = true;
		state.selectPadMode(3, 'pad-fx-1');
		assert.equal(state.padModeFor(3), 'pad-fx-1');
		port.sendBeatFxUnitCommand('3', REL_OFF);
		assert.deepEqual(sent, [REL_OFF]);
		state.selectPadMode(3, 'hot-cue');
	} finally {
		uninstall();
	}
});

test('PadFxBank SSR renders preset labels while the beat FX unit is unwired', async () => {
	const ssr = await loadSvelteSsrModule(PAD_FX_BANK_SSR);
	const body = ssr.render(ssr.PadFxBank, { props: { deckId: 1, mode: 'pad-fx-1' } }).body;
	assert.match(body, /data-pad-fx-mode="pad-fx-1"/);
	assert.match(body, /data-testid="pad-fx-deck-1-pad-0"/);
	assert.match(body, /ECHO 1\/2/);
});

test('source wiring: HotCueBank branches to PadFxBank; pads release on up/cancel/leave; menus select', () => {
	const hot = src('src/lib/components/rb/deck/HotCueBank.svelte');
	assert.match(
		hot,
		/\{#if isPadFxModeId\(padMode\)\}\s*<PadFxBank deckId=\{deck\.deck_id\} mode=\{padMode\} \/>/
	);
	const bank = src('src/lib/components/rb/deck/PadFxBank.svelte');
	for (const h of [
		'onpointerdown',
		'onpointerup',
		'onpointercancel',
		'onpointerleave',
		'ArrowLeft',
		'ArrowRight',
		'releaseAll'
	])
		assert.ok(bank.includes(h), h);
	assert.doesNotMatch(bank, /setPointerCapture/);
	for (const twin of ['PadModeMenu.svelte', 'PadModeMenu.tsx']) {
		const menu = src(`src/lib/components/rb/deck/${twin}`);
		assert.match(menu, /padModeSelectable\(deckId, entry\)/, twin);
		assert.match(
			menu,
			/runPerformanceCommandFromUi\(\{\s*type: 'pad_mode',\s*deck: deckId,\s*mode: entry\.id\s*\}\)/,
			twin
		);
	}
});

// [if] PadFxBank declares the controller as $state [then] its mount $effect writes then reads it and loops (effect_update_depth_exceeded)
test('PadFxBank controller is a plain let, not $state, so the mount effect cannot re-trigger itself', () => {
	const bank = src('src/lib/components/rb/deck/PadFxBank.svelte');
	assert.match(bank, /let controller: PadFxController \| null = null;/);
	assert.doesNotMatch(bank, /let controller[^\n]*\$state/);
});
