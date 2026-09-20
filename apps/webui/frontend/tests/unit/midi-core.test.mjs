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
let stores; // $lib/stores.svelte
let audioEngine; // $lib/rb/audio-engine.svelte
let performanceIpc; // $lib/rb/performance-ipc.svelte

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
	stores = await vite.ssrLoadModule('/src/lib/stores.svelte.ts');
	audioEngine = await vite.ssrLoadModule('/src/lib/rb/audio-engine.svelte.ts');
	performanceIpc = await vite.ssrLoadModule('/src/lib/rb/performance-ipc.svelte.ts');
});

after(async () => {
	await vite.close();
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

test('mixer_global actions drive mixerState directly', () => {
	glue.handleMidiAction(
		{ type: 'mixer_global', target: 'master' },
		{ kind: 'continuous', value01: 0.5, raw: 64 }
	);
	assert.equal(audioEngine.mixerState.master, 0.5);
	glue.handleMidiAction(
		{ type: 'mixer_global', target: 'crossfader' },
		{ kind: 'continuous', value01: 0.25, raw: 32 }
	);
	assert.equal(audioEngine.mixerState.crossfader, 0.25);
});

test('mixer_channel actions drive the channel strip state', () => {
	glue.handleMidiAction(
		{ type: 'mixer_channel', deck: 2, target: 'trim' },
		{ kind: 'continuous', value01: 0.75, raw: 95 }
	);
	assert.equal(audioEngine.mixerState.channels[2].trim, 0.75);
	glue.handleMidiAction(
		{ type: 'mixer_channel', deck: 2, target: 'eq', band: 'low' },
		{ kind: 'continuous', value01: 0.1, raw: 13 }
	);
	assert.equal(audioEngine.mixerState.channels[2].eq_low, 0.1);
	glue.handleMidiAction(
		{ type: 'mixer_channel', deck: 4, target: 'fader' },
		{ kind: 'continuous14', value01: 0.5, raw: 8192 }
	);
	assert.equal(audioEngine.mixerState.channels[4].fader, 0.5);
});

// Regression: channel_cue used to call engine.setChannelCue directly,
// bypassing the shared performance dispatcher - so a controller press could
// mutate PFL state while a preset lifecycle lock had every other deck-scoped
// command rejected. Route it through the dispatcher (same command
// Mixer.svelte's on-screen CUE button sends) and prove a held lock now
// rejects the press exactly like it would a UI click, instead of applying
// silently underneath it.
//   if channel_cue still flips mixerState while a preset lock is held then
//   the direct-call bypass is back
test('channel_cue press is routed through the dispatcher and respects the preset lifecycle lock', async () => {
	audioEngine.mixerState.channels[2].cue_enabled = false;
	const beforeToasts = stores.toasts.length;

	// The dispatcher requires an active command session (same precondition a
	// real /performance mount satisfies via installPerformanceBrowserIpc) -
	// without it dispatchPerformanceCommand throws for an unrelated "no
	// session" reason and the preset-lock assertion below would be vacuous.
	globalThis.window = {};
	const uninstallIpc = performanceIpc.installPerformanceBrowserIpc();
	try {
		await performanceIpc.preparePerformancePresetTransaction(
			'test-778-channel-cue-lock',
			async () => {
				glue.handleMidiAction(
					{ type: 'channel_cue', deck: 2 },
					{ kind: 'button', pressed: true, velocity: 127 }
				);
				// handleMidiAction fires the dispatch and returns without awaiting
				// it (same fire-and-forget shape as every other UI click handler);
				// give its rejection a couple of microtask turns to land before
				// checking that the lock actually held.
				await Promise.resolve();
				await Promise.resolve();
				assert.equal(
					audioEngine.mixerState.channels[2].cue_enabled,
					false,
					'a direct engine call would have flipped cue_enabled even under an active preset lock'
				);
			}
		);
		// Clean, real release path (mirrors an aborted preset boot) so the
		// claim does not leak into later tests in this file.
		performanceIpc.abortPreparedPerformancePreset('test-778-channel-cue-lock', 'test cleanup');

		assert.equal(
			audioEngine.mixerState.channels[2].cue_enabled,
			false,
			'the rejected command must never have applied, lock released or not'
		);
		assert.match(
			performanceIpc.performanceCommandStatus.deck_errors[2] ?? '',
			/owns controls/,
			'the dispatcher never even saw the command, so nothing proves it was routed there'
		);
		// +2: the routed-and-rejected channel_cue command, then the cleanup
		// abort above - each rejection surfaces its own toast.
		assert.equal(
			stores.toasts.length,
			beforeToasts + 2,
			'a routed-and-rejected command must surface a toast'
		);
	} finally {
		uninstallIpc();
		delete globalThis.window;
	}
});

// Regression: the deck-command adapters (_cmdPlayToggle/_cmdPressCue/
// _cmdHotCue/_cmdBeatLoop/_cmdLoopExit) used to call engine.play/pause/
// pressCue/cueJump/engageBeatLoop/setLoop directly, the same bypass fixed
// for channel_cue above. Prove each is routed through the dispatcher by the
// same preset-lifecycle-lock probe: a direct call would never touch the
// dispatcher, so deck_errors[deck] would stay unset instead of recording
// the lock rejection.
//   if any of the five stop reaching the dispatcher then this goes red
test('deck command adapters are routed through the dispatcher and respect the preset lifecycle lock', async () => {
	audioEngine.deckStates[1].stable_id = 'a'.repeat(40);
	audioEngine.deckStates[1].hot_cues = [
		{ slot: 'A', in_ms: 1000, out_ms: null, is_loop: false, color_table_index: null, comment: null }
	];
	const actions = [
		{ type: 'deck_play_toggle', deck: 1 },
		{ type: 'deck_cue', deck: 1 },
		{ type: 'deck_hot_cue', deck: 1, slot: 'A' },
		{ type: 'deck_beat_loop', deck: 1, beats: 4 },
		{ type: 'deck_loop_exit', deck: 1 }
	];

	globalThis.window = {};
	const uninstallIpc = performanceIpc.installPerformanceBrowserIpc();
	try {
		await performanceIpc.preparePerformancePresetTransaction(
			'test-511-deck-command-lock',
			async () => {
				for (const action of actions) {
					performanceIpc.performanceCommandStatus.deck_errors[1] = null;
					glue.handleMidiAction(action, { kind: 'button', pressed: true, velocity: 127 });
					await Promise.resolve();
					await Promise.resolve();
					assert.match(
						performanceIpc.performanceCommandStatus.deck_errors[1] ?? '',
						/owns controls/,
						`${action.type}: the dispatcher never saw the command, so a direct engine call is back`
					);
				}
			}
		);
		performanceIpc.abortPreparedPerformancePreset('test-511-deck-command-lock', 'test cleanup');
	} finally {
		uninstallIpc();
		delete globalThis.window;
		audioEngine.deckStates[1].stable_id = null;
		audioEngine.deckStates[1].hot_cues = [];
	}
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

test('browse actions without a BrowseAdapter are loud (toast), not silent', () => {
	const beforeCount = stores.toasts.length;
	glue.handleMidiAction({ type: 'browse_encoder' }, { kind: 'relative', delta: 3 });
	assert.equal(stores.toasts.length, beforeCount + 1);
	assert.match(lastToast().message, /browser not ready/);
});

test('registerBrowseAdapter wires encoder + load and rejects doubles', () => {
	const calls = [];
	glue.registerBrowseAdapter({
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
});

// ---------------------------------------------------- glue lifecycle

// Meter pump regression guard intentionally absent on main (issue #3670):
// host-driven VU meter CC output is planned (controller-onboarding.md,
// unlanded bdf50f0e) but not implemented. Restoring the deleted test would
// require building the pump, not restoring coverage. Track there, not here.

// Regression, PR #509 review thread r3913374758 ("Reattach MIDI glue after
// returning to performance"): detachMidiGlueForRouteUnmount() only ever
// stopped the LED effect + meter pump, never webmidi's action-handler
// registration - so a SECOND attachMidiGlue() call (leaving and returning to
// /performance while MIDI stays granted) re-invoked registerActionHandler()
// and threw "a handler is already registered", leaving every mapped control
// dead until a full page reload.
//
//   if a second attachMidiGlue() call after a prior detach throws
//   then broken (returning to /performance permanently kills MIDI)
test('attachMidiGlue can reattach after a prior detach without throwing', () => {
	webmidi._resetMidiForTests();
	const detach1 = glue.attachMidiGlue();
	detach1();
	assert.doesNotThrow(() => {
		const detach2 = glue.attachMidiGlue();
		detach2();
	}, 'a second attachMidiGlue() after detach must not throw');
	webmidi._resetMidiForTests();
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

test('registerDeviceMap rejects a second map on the same nameMatch AND tier', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(_testMap('FirstCo', 'TieredTest', 11), 'installed');
	assert.throws(
		() => webmidi.registerDeviceMap(_testMap('SecondCo', 'TieredTest', 12), 'installed'),
		/already registered/
	);
	webmidi._resetMidiForTests();
});

test('unregisterActionHandler lets the glue re-attach after a teardown', () => {
	webmidi._resetMidiForTests();
	const detach = glue.attachMidiGlue();
	detach();
	// Before unregisterActionHandler existed this threw: the teardown cleared
	// its own latch but left the handler registered, so a /performance remount
	// could never re-attach.
	const again = glue.attachMidiGlue();
	again();
	webmidi._resetMidiForTests();
});
