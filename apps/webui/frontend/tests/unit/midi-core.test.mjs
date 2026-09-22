// Unit tests for the midi core (midi-types contract consumers:
// webmidi.svelte.ts decode/pairing/map validation + action-glue engine
// wiring). Rune modules need the svelte plugin, so this bootstrap differs
// from beat-sync-math.test.mjs: plugins [svelte()] + a $lib alias.
//
// Regression lines (single-line format per CLAUDE.md):
//   if decodeSource(0x91,60) isn't {ch:2,kind:'note',id:60} then broken
//   if decodeRelative(127) isn't -1 (spike 2a CCW tick) then broken
//   if combine14(0x40,0) isn't 8192 then broken
//   if pitchRatioFromFader(1,16) isn't 1.16 then broken
//   if duplicate DeviceMap bindings don't throw then broken
//   if play on an empty deck doesn't toast (and doesn't stay silent) then broken
//   if mixer_global master 0.5 doesn't hit mixerState.master then broken
//   if eq action without band doesn't throw then broken
//   if ledTriggerActive(deck_loaded) is true on an empty deck then broken
//   if the glue teardown leaves the action handler registered then broken

import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

let vite;
let webmidi; // webmidi.svelte.ts module
let glue; // action-glue.svelte.ts module
let padRuntime; // controller-pad-runtime.svelte.ts module
let stores; // $lib/stores.svelte
let audioEngine; // $lib/rb/audio-engine.svelte
let performanceIpc;
let uninstallPerformanceIpc;

before(async () => {
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true },
		plugins: [svelte()],
		resolve: {
			alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') },
			conditions: ['browser']
		}
	});
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
	glue = await vite.ssrLoadModule('/src/lib/rb/midi/action-glue.svelte.ts');
	padRuntime = await vite.ssrLoadModule('/src/lib/rb/midi/controller-pad-runtime.svelte.ts');
	stores = await vite.ssrLoadModule('/src/lib/stores.svelte.ts');
	audioEngine = await vite.ssrLoadModule('/src/lib/rb/audio-engine.svelte.ts');
	performanceIpc = await vite.ssrLoadModule('/src/lib/rb/performance-ipc.svelte.ts');
	globalThis.window = {};
	uninstallPerformanceIpc = performanceIpc.installPerformanceBrowserIpc();
});

// A failed before() leaves later fields unset. The Vite server must still
// close, or its open handles keep node --test alive and hang the whole unit
// suite (Tue 15 Sep 2026: a module that failed to load did exactly that).
after(async () => {
	try {
		uninstallPerformanceIpc?.();
		delete globalThis.window;
	} finally {
		await vite?.close();
	}
});

// ------------------------------------------------------------ wire decode

test('decodeSource decodes note/cc/pitchbend and rejects other families', () => {
	assert.deepEqual(webmidi.decodeSource(0x91, 60), { ch: 2, kind: 'note', id: 60 });
	assert.deepEqual(webmidi.decodeSource(0x80, 60), { ch: 1, kind: 'note', id: 60 });
	assert.deepEqual(webmidi.decodeSource(0xb3, 31), { ch: 4, kind: 'cc', id: 31 });
	assert.deepEqual(webmidi.decodeSource(0xe0, 0), { ch: 1, kind: 'pitchbend', id: 0 });
	assert.equal(webmidi.decodeSource(0xc0, 5), null); // program change: out of P0
	assert.equal(webmidi.decodeSource(0xd0, 5), null); // channel pressure
});

test('decodeRelative matches the spike 2a encoder tick ranges', () => {
	// CW ticks 0x01..0x1E -> +1..+30
	assert.equal(webmidi.decodeRelative(1), 1);
	assert.equal(webmidi.decodeRelative(30), 30);
	// CCW ticks 0x7F..0x62 -> -1..-30
	assert.equal(webmidi.decodeRelative(127), -1);
	assert.equal(webmidi.decodeRelative(98), -30);
});

test('combine14 builds MSB<<7|LSB', () => {
	assert.equal(webmidi.combine14(0x40, 0x00), 8192);
	assert.equal(webmidi.combine14(0, 0), 0);
	assert.equal(webmidi.combine14(127, 127), 16383);
});

// ------------------------------------------------------- map registration

test('registerDeviceMap fails fast on duplicate bindings', () => {
	webmidi._resetMidiForTests();
	const src = { ch: 1, kind: 'note', id: 11 };
	assert.throws(
		() =>
			webmidi.registerDeviceMap({
				vendor: 'testvendor',
				nameMatch: 'TestDevice',
				bindings: [
					{ source: src, action: { type: 'deck_play_toggle', deck: 1 } },
					{ source: src, action: { type: 'deck_cue', deck: 1 } }
				]
			}),
		/duplicate binding/
	);
});

test('registerDeviceMap fails fast on 14-bit pitch bound to a note source', () => {
	webmidi._resetMidiForTests();
	assert.throws(
		() =>
			webmidi.registerDeviceMap({
				vendor: 'testvendor',
				nameMatch: 'TestDevice',
				bindings: [
					{
						source: { ch: 1, kind: 'note', id: 11 },
						action: { type: 'deck_pitch', deck: 1, lsbOffset: 32 }
					}
				]
			}),
		/must bind a cc source/
	);
});

test('registerDeviceMap fails fast on empty or invalid nameMatch', () => {
	webmidi._resetMidiForTests();
	assert.throws(
		() => webmidi.registerDeviceMap({ vendor: 'v', nameMatch: '', bindings: [] }),
		/non-empty/
	);
	assert.throws(() =>
		webmidi.registerDeviceMap({ vendor: 'v', nameMatch: '[', bindings: [] })
	);
});

test('registerActionHandler rejects a second registration', () => {
	webmidi._resetMidiForTests();
	webmidi.registerActionHandler(() => {});
	assert.throws(() => webmidi.registerActionHandler(() => {}), /already registered/);
});

test('initMidi without WebMIDI support marks unsupported and throws', async () => {
	webmidi._resetMidiForTests();
	// node has no navigator.requestMIDIAccess
	await assert.rejects(() => webmidi.initMidi(), /not supported/);
	assert.equal(webmidi.midiState.permission, 'unsupported');
});

// ------------------------------------------------------------ action glue

function lastToast() {
	const toasts = stores.toasts;
	return toasts.length === 0 ? null : toasts[toasts.length - 1];
}

test('pitchRatioFromFader maps 0..1 onto the +-range window', () => {
	assert.equal(glue.pitchRatioFromFader(0.5, 16), 1);
	assert.equal(glue.pitchRatioFromFader(1, 16), 1.16);
	assert.equal(glue.pitchRatioFromFader(0, 8), 0.92);
	assert.throws(() => glue.pitchRatioFromFader(1.5, 16), RangeError);
	assert.throws(() => glue.pitchRatioFromFader(Number.NaN, 16), RangeError);
});

test('midiMeterValue maps the real ten-segment meter onto Mixtour Pro 0..6', () => {
	assert.equal(glue.midiMeterValue(0, 6), 0);
	assert.equal(glue.midiMeterValue(5, 6), 3);
	assert.equal(glue.midiMeterValue(10, 6), 6);
	assert.throws(() => glue.midiMeterValue(11, 6), RangeError);
});

test('transport action on an empty deck toasts instead of throwing', () => {
	const beforeCount = stores.toasts.length;
	glue.handleMidiAction(
		{ type: 'deck_play_toggle', deck: 1 },
		{ kind: 'button', pressed: true, velocity: 127 }
	);
	assert.equal(stores.toasts.length, beforeCount + 1);
	assert.match(lastToast().message, /Deck 1 is empty/);
});

test('button releases are ignored (no toast, no engine call)', () => {
	const beforeCount = stores.toasts.length;
	glue.handleMidiAction(
		{ type: 'deck_cue', deck: 2 },
		{ kind: 'button', pressed: false, velocity: 0 }
	);
	assert.equal(stores.toasts.length, beforeCount);
});

test('hot cue press on an empty slot toasts the missing-slot state', () => {
	// Deck 3 stays empty in these tests: empty-deck toast comes first.
	glue.handleMidiAction(
		{ type: 'deck_hot_cue', deck: 3, slot: 'A' },
		{ kind: 'button', pressed: true, velocity: 127 }
	);
	assert.match(lastToast().message, /Deck 3 is empty/);
});

test('mixer_global actions dispatch through the performance command bus', async () => {
	glue.handleMidiAction(
		{ type: 'mixer_global', target: 'master' },
		{ kind: 'continuous', value01: 0.5, raw: 64 }
	);
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(audioEngine.mixerState.master, 0.5);
	glue.handleMidiAction(
		{ type: 'mixer_global', target: 'crossfader' },
		{ kind: 'continuous', value01: 0.25, raw: 32 }
	);
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(audioEngine.mixerState.crossfader, 0.25);
});

test('mixer_channel actions dispatch through the performance command bus', async () => {
	glue.handleMidiAction(
		{ type: 'mixer_channel', deck: 2, target: 'trim' },
		{ kind: 'continuous', value01: 0.75, raw: 95 }
	);
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(audioEngine.mixerState.channels[2].trim, 0.75);
	glue.handleMidiAction(
		{ type: 'mixer_channel', deck: 2, target: 'eq', band: 'low' },
		{ kind: 'continuous', value01: 0.1, raw: 13 }
	);
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(audioEngine.mixerState.channels[2].eq_low, 0.1);
	glue.handleMidiAction(
		{ type: 'mixer_channel', deck: 4, target: 'fader' },
		{ kind: 'continuous14', value01: 0.5, raw: 8192 }
	);
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(audioEngine.mixerState.channels[4].fader, 0.5);
});

test('eq action without band fails fast', () => {
	assert.throws(
		() =>
			glue.handleMidiAction(
				{ type: 'mixer_channel', deck: 1, target: 'eq' },
				{ kind: 'continuous', value01: 0.5, raw: 64 }
			),
		/requires band/
	);
});

test('button action fed continuous input fails fast', () => {
	assert.throws(
		() =>
			glue.handleMidiAction(
				{ type: 'deck_play_toggle', deck: 1 },
				{ kind: 'continuous', value01: 0.5, raw: 64 }
			),
		/non-button input/
	);
});

test('shift_modifier reaching the glue is a wiring bug and throws', () => {
	assert.throws(
		() =>
			glue.handleMidiAction(
				{ type: 'shift_modifier' },
				{ kind: 'button', pressed: true, velocity: 127 }
			),
		/must never reach/
	);
});

test('ledTriggerActive reflects deck store state', () => {
	assert.equal(glue.ledTriggerActive({ kind: 'deck_loaded', deck: 1 }), false);
	assert.equal(glue.ledTriggerActive({ kind: 'deck_playing', deck: 1 }), false);
	assert.equal(glue.ledTriggerActive({ kind: 'loop_engaged', deck: 1 }), false);
	assert.equal(glue.ledTriggerActive({ kind: 'hot_cue_present', deck: 1, slot: 'A' }), false);
	// Flip real store state and re-check (deep-reactive $state proxy).
	audioEngine.deckStates[1].stable_id = 'a'.repeat(40);
	audioEngine.deckStates[1].hot_cues = [
		{ slot: 'A', in_ms: 1000, out_ms: null, is_loop: false, color_table_index: null, comment: null }
	];
	assert.equal(glue.ledTriggerActive({ kind: 'deck_loaded', deck: 1 }), true);
	assert.equal(glue.ledTriggerActive({ kind: 'hot_cue_present', deck: 1, slot: 'A' }), true);
	assert.equal(glue.ledTriggerActive({ kind: 'hot_cue_present', deck: 1, slot: 'B' }), false);
	// Restore the empty-deck state for any later tests.
	audioEngine.deckStates[1].stable_id = null;
	audioEngine.deckStates[1].hot_cues = [];
});

test('controller pad mode is per device/deck and drives mode LED truth', () => {
	glue._resetControllerStateForTests();
	assert.equal(glue.controllerPadMode('pro-a', 1), 'hot_cue');
	glue.handleMidiAction(
		{ type: 'controller_pad_mode', deck: 1, mode: 'auto_loop' },
		{ kind: 'button', pressed: true, velocity: 127 },
		'pro-a'
	);
	assert.equal(glue.controllerPadMode('pro-a', 1), 'auto_loop');
	assert.equal(glue.controllerPadMode('pro-a', 2), 'hot_cue');
	assert.equal(glue.controllerPadMode('pro-b', 1), 'hot_cue');
	assert.equal(
		glue.ledTriggerActive({ kind: 'pad_mode_selected', deck: 1, mode: 'auto_loop' }, 'pro-a'),
		true
	);
	assert.equal(
		glue.ledTriggerActive({ kind: 'pad_mode_selected', deck: 1, mode: 'hot_cue' }, 'pro-a'),
		false
	);
	glue._resetControllerStateForTests();
});

test('controller hot-cue pad rounds the presented position to persistent milliseconds', () => {
	assert.equal(padRuntime.persistentCuePositionMs(1234.75), 1235);
	assert.equal(padRuntime.persistentCuePositionMs(0.4), 0);
	assert.throws(() => padRuntime.persistentCuePositionMs(Number.NaN), RangeError);
	assert.throws(() => padRuntime.persistentCuePositionMs(-0.1), RangeError);
});

test('unsupported pad mode warns once and pad input stays inert', () => {
	glue._resetControllerStateForTests();
	const beforeCount = stores.toasts.length;
	glue.handleMidiAction(
		{ type: 'controller_pad_mode', deck: 2, mode: 'instant_fx' },
		{ kind: 'button', pressed: true, velocity: 127 },
		'pro-a'
	);
	assert.equal(stores.toasts.length, beforeCount + 1);
	assert.match(lastToast().message, /instant fx pads are not available/);
	const afterWarning = stores.toasts.length;
	glue.handleMidiAction(
		{ type: 'controller_pad', deck: 2, pad: 1, shifted: false },
		{ kind: 'button', pressed: true, velocity: 127 },
		'pro-a'
	);
	// Empty-deck protection remains visible, but no unrelated hot-cue or loop
	// action is dispatched. A loaded deck is covered by the static mode/action
	// map test and physical acceptance.
	assert.equal(stores.toasts.length, afterWarning + 1);
	assert.match(lastToast().message, /Deck 2 is empty/);
	glue._resetControllerStateForTests();
});

test('browse actions without a BrowseAdapter are loud (toast), not silent', () => {
	const beforeCount = stores.toasts.length;
	glue.handleMidiAction({ type: 'browse_encoder' }, { kind: 'relative', delta: 3 });
	assert.equal(stores.toasts.length, beforeCount + 1);
	assert.match(lastToast().message, /browser not ready/);
});

test('registerBrowseAdapter wires encoder + load and rejects doubles', () => {
	const calls = [];
	const unregister = glue.registerBrowseAdapter({
		moveSelection: (delta) => calls.push(['move', delta]),
		loadSelected: (deck) => calls.push(['load', deck])
	});
	glue.handleMidiAction({ type: 'browse_encoder' }, { kind: 'relative', delta: -2 });
	glue.handleMidiAction(
		{ type: 'browse_load', deck: 2 },
		{ kind: 'button', pressed: true, velocity: 127 }
	);
	assert.deepEqual(calls, [
		['move', -2],
		['load', 2]
	]);
	assert.throws(() => glue.registerBrowseAdapter({ moveSelection: () => {}, loadSelected: () => {} }), /already registered/);
	unregister();
	const unregisterRemount = glue.registerBrowseAdapter({ moveSelection: () => {}, loadSelected: () => {} });
	unregisterRemount();
});

function _restoreCueGlueState() {
	audioEngine.mixerState.channels[2].cue_enabled = false;
	audioEngine.mixerState.headphones.mix = 0.5;
	audioEngine.mixerState.headphones.level = 0.5;
	glue._resetMasterCueForTests();
}

test('channel_cue press toggles cue_enabled; release is ignored', async () => {
	_restoreCueGlueState();
	assert.equal(audioEngine.mixerState.channels[2].cue_enabled, false);
	glue.handleMidiAction(
		{ type: 'channel_cue', deck: 2 },
		{ kind: 'button', pressed: true, velocity: 127 }
	);
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(audioEngine.mixerState.channels[2].cue_enabled, true);
	glue.handleMidiAction(
		{ type: 'channel_cue', deck: 2 },
		{ kind: 'button', pressed: false, velocity: 0 }
	);
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(audioEngine.mixerState.channels[2].cue_enabled, true);
	assert.equal(glue.ledTriggerActive({ kind: 'channel_cue_enabled', deck: 2 }), true);
	_restoreCueGlueState();
});

// ---------------------------------------------------- glue lifecycle

// Meter pump regression guard intentionally absent on main (issue #3670):
// host-driven VU meter CC output is planned (controller-onboarding.md,
// unlanded bdf50f0e) but not implemented. Restoring the deleted test would
// require building the pump, not restoring coverage. Track there, not here.

test('headphone_mix and headphone_level dispatch through the performance command bus', async () => {
	_restoreCueGlueState();
	glue.handleMidiAction(
		{ type: 'headphone_mix' },
		{ kind: 'continuous', value01: 0.25, raw: 32 }
	);
	glue.handleMidiAction(
		{ type: 'headphone_level' },
		{ kind: 'continuous', value01: 0.75, raw: 95 }
	);
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(audioEngine.mixerState.headphones.mix, 0.25);
	assert.equal(audioEngine.mixerState.headphones.level, 0.75);
	_restoreCueGlueState();
});

test('master_cue latch forces mix to 1 then restores on second press', async () => {
	_restoreCueGlueState();
	audioEngine.mixerState.headphones.mix = 0.3;
	glue.handleMidiAction(
		{ type: 'master_cue', mode: 'latch' },
		{ kind: 'button', pressed: true, velocity: 127 }
	);
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(audioEngine.mixerState.headphones.mix, 1);
	glue.handleMidiAction(
		{ type: 'master_cue', mode: 'latch' },
		{ kind: 'button', pressed: false, velocity: 0 }
	);
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(audioEngine.mixerState.headphones.mix, 1);
	glue.handleMidiAction(
		{ type: 'master_cue', mode: 'latch' },
		{ kind: 'button', pressed: true, velocity: 127 }
	);
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(audioEngine.mixerState.headphones.mix, 0.3);
	_restoreCueGlueState();
});

test('master_cue hold engages on press and restores on release', async () => {
	_restoreCueGlueState();
	audioEngine.mixerState.headphones.mix = 0.4;
	glue.handleMidiAction(
		{ type: 'master_cue', mode: 'hold' },
		{ kind: 'button', pressed: true, velocity: 127 }
	);
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(audioEngine.mixerState.headphones.mix, 1);
	glue.handleMidiAction(
		{ type: 'master_cue', mode: 'hold' },
		{ kind: 'button', pressed: false, velocity: 0 }
	);
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(audioEngine.mixerState.headphones.mix, 0.4);
	_restoreCueGlueState();
});

test('headphone_mix while master_cue latched updates saved restore mix only', async () => {
	_restoreCueGlueState();
	audioEngine.mixerState.headphones.mix = 0.2;
	glue.handleMidiAction(
		{ type: 'master_cue', mode: 'latch' },
		{ kind: 'button', pressed: true, velocity: 127 }
	);
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(audioEngine.mixerState.headphones.mix, 1);
	glue.handleMidiAction(
		{ type: 'headphone_mix' },
		{ kind: 'continuous', value01: 0.8, raw: 102 }
	);
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(audioEngine.mixerState.headphones.mix, 1);
	glue.handleMidiAction(
		{ type: 'master_cue', mode: 'latch' },
		{ kind: 'button', pressed: true, velocity: 127 }
	);
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(audioEngine.mixerState.headphones.mix, 0.8);
	_restoreCueGlueState();
});

// -------------------------------------------- tiered registry (installed maps)

// A controller onboarded at runtime has to be able to override a built-in map
// without editing the bundle, and to be removed again without a page reload.
// Precedence is TIERED, not array-order: installed always beats builtin, so
// resolution does not depend on which module happened to register first.
// Design: specs/controller-onboarding.md section 3.3.
//
//   if an installed map loses to a builtin on the same port name then broken
//   if unregisterDeviceMap leaves the map resolvable then broken
//   if unregisterDeviceMap reports a removal for a nameMatch nobody registered
//     then broken (a silent no-op hides a bad uninstall)

const _testMap = (vendor, nameMatch, note) => ({
	vendor,
	nameMatch,
	bindings: [{ source: { ch: 1, kind: 'note', id: note }, action: { type: 'deck_cue', deck: 1 } }]
});

// Kept from 76dcf21ba (#3643): these cover installed-maps.ts, which that
// commit really did add. The rest of this file is 76dcf21ba~1's, because the
// same commit re-imported an older copy over it (see the PR description).

test('installed maps shadow builtin maps regardless of registration order', () => {
	webmidi._resetMidiForTests();
	// Builtin registered FIRST, installed second: installed must still win.
	webmidi.registerDeviceMap(_testMap('BuiltinCo', 'TieredTest', 11), 'builtin');
	webmidi.registerDeviceMap(_testMap('InstalledCo', 'TieredTest', 12), 'installed');
	assert.equal(webmidi.resolveMapForPort('TieredTest 1')?.vendor, 'InstalledCo');

	// And the reverse order resolves identically.
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(_testMap('InstalledCo', 'TieredTest', 12), 'installed');
	webmidi.registerDeviceMap(_testMap('BuiltinCo', 'TieredTest', 11), 'builtin');
	assert.equal(webmidi.resolveMapForPort('TieredTest 1')?.vendor, 'InstalledCo');
	webmidi._resetMidiForTests();
});

test('listDeviceMaps reports tier so the UI can name what shadows what', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(_testMap('BuiltinCo', 'TieredTest', 11), 'builtin');
	webmidi.registerDeviceMap(_testMap('InstalledCo', 'TieredTest', 12), 'installed');
	const listed = webmidi.listDeviceMaps().map((e) => `${e.tier}:${e.map.vendor}`).sort();
	assert.deepEqual(listed, ['builtin:BuiltinCo', 'installed:InstalledCo']);
	webmidi._resetMidiForTests();
});

test('registerDeviceMap rejects a second map on the same nameMatch AND tier', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(_testMap('FirstCo', 'TieredTest', 11), 'installed');
	assert.throws(
		() => webmidi.registerDeviceMap(_testMap('SecondCo', 'TieredTest', 12), 'installed'),
		/already registered/
	);
	webmidi._resetMidiForTests();
});

test('unregisterDeviceMap removes one tier and reveals the map it shadowed', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(_testMap('BuiltinCo', 'TieredTest', 11), 'builtin');
	webmidi.registerDeviceMap(_testMap('InstalledCo', 'TieredTest', 12), 'installed');
	assert.equal(webmidi.unregisterDeviceMap('TieredTest', 'installed'), true);
	// The builtin was never removed, so uninstalling reverts rather than breaks.
	assert.equal(webmidi.resolveMapForPort('TieredTest 1')?.vendor, 'BuiltinCo');
	assert.equal(webmidi.unregisterDeviceMap('TieredTest', 'builtin'), true);
	assert.equal(webmidi.resolveMapForPort('TieredTest 1'), null);
	webmidi._resetMidiForTests();
});

test('unregisterDeviceMap returns false rather than pretending it removed one', () => {
	webmidi._resetMidiForTests();
	assert.equal(webmidi.unregisterDeviceMap('NeverRegistered', 'installed'), false);
	webmidi._resetMidiForTests();
});

// A MIDI-enabled user who leaves /performance and comes back is the whole
// point of these two: detachMidiGlueForRouteUnmount() drops the glue, and
// the next requestMidiAccess() re-attaches. unregisterActionHandler()'s own
// docstring says the teardown calls it, and for a while nothing did, so the
// second attach hit registerActionHandler()'s already-registered throw. That
// throw surfaces inside requestMidiAccess()'s catch, which also runs
// setMidiEnabledChoice(false) -- so the remount did not merely fail, it
// silently forgot the user's MIDI opt-in and mapped controls stayed dead
// until a full page reload.
test('the glue teardown releases the action handler, not just its own latch', () => {
	webmidi._resetMidiForTests();
	const detach = glue.attachMidiGlue();
	// Asserted on BOTH sides on purpose: against the post-detach check alone,
	// an accessor stubbed to a flat false passes while proving nothing.
	assert.equal(
		webmidi._actionHandlerRegisteredForTests(),
		true,
		'attach must register a handler for the teardown assertion below to mean anything'
	);
	detach();
	assert.equal(
		webmidi._actionHandlerRegisteredForTests(),
		false,
		'teardown must release the handler webmidi holds, or the next attach throws'
	);
	webmidi._resetMidiForTests();
});

test('attachMidiGlue can reattach after a detach, as a route remount does', () => {
	webmidi._resetMidiForTests();
	glue.attachMidiGlue()();
	assert.doesNotThrow(() => {
		glue.attachMidiGlue()();
	}, 'returning to /performance must re-attach rather than throw');
	webmidi._resetMidiForTests();
});
