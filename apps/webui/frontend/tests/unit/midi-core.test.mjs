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
