// Unit tests for the MIDI panel unit (midi-format pure helpers +
// midi-ui-state rune module). Same vite bootstrap as midi-core.test.mjs:
// rune modules need the svelte plugin + $lib alias.
//
// Regression lines (single-line format per CLAUDE.md):
//   if hexByte(0x90) isn't '90' or hexByte(300) doesn't throw then broken
//   if formatBytes(0x90,60,127) isn't '90 3C 7F' then broken
//   if describeSource(null) doesn't say 'undecoded' then broken
//   if midiLabelStatus(granted, no mapped device) is green then broken
//   if midiLabelStatus with requestPending and MIDI on isn't amber then broken
//   if midiLabelStatus(granted, MIDI turned off) is red then broken
//   if midiLabelStatus(pending prompt, MIDI turned off) is amber then broken
//   if requestMidiAccess failure leaves midiUi.lastError null then broken
//   if two toggleMidiPanel calls don't restore panelOpen then broken

import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

let vite;
let fmt; // midi-format.ts (pure)
let uiState; // midi-ui-state.svelte.ts (rune module)
let enabledChoice; // midi-enabled-choice.ts (the persisted opt-in, runtime-free)
let webmidi; // webmidi.svelte.ts (for permission state assertions)
let flx10; // ddj-flx10.ts (real map, for best-guess-hint transcription check)

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
	fmt = await vite.ssrLoadModule('/src/lib/components/rb/midi/midi-format.ts');
	uiState = await vite.ssrLoadModule('/src/lib/components/rb/midi/midi-ui-state.svelte.ts');
	// The key lives in midi-enabled-choice.ts, which is its only home: prefs
	// hydration reads it there without dragging in this module's MIDI runtime.
	enabledChoice = await vite.ssrLoadModule(
		'/src/lib/components/rb/midi/midi-enabled-choice.ts'
	);
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
	flx10 = await vite.ssrLoadModule('/src/lib/rb/midi/maps/ddj-flx10.ts');
});

/** A Map-backed localStorage stub installed on globalThis for the persistence
 * tests (Node has no localStorage; the module guards on its absence). */
function _installLocalStorage(initial = {}) {
	const store = new Map(Object.entries(initial));
	globalThis.localStorage = {
		getItem: (k) => (store.has(k) ? store.get(k) : null),
		setItem: (k, v) => store.set(k, String(v)),
		removeItem: (k) => store.delete(k),
		clear: () => store.clear()
	};
	return store;
}

function _uninstallLocalStorage() {
	delete globalThis.localStorage;
}

after(async () => {
	uiState?.detachMidiGlueForRouteUnmount();
	await vite.close();
});

// ------------------------------------------------------------- midi-format

test('hexByte renders two uppercase hex chars and rejects non-bytes', () => {
	assert.equal(fmt.hexByte(0x90), '90');
	assert.equal(fmt.hexByte(7), '07');
	assert.equal(fmt.hexByte(0), '00');
	assert.equal(fmt.hexByte(255), 'FF');
	assert.throws(() => fmt.hexByte(256), RangeError);
	assert.throws(() => fmt.hexByte(-1), RangeError);
	assert.throws(() => fmt.hexByte(1.5), RangeError);
});

test('formatBytes renders the learn-log byte triple', () => {
	assert.equal(fmt.formatBytes(0x90, 60, 127), '90 3C 7F');
	assert.equal(fmt.formatBytes(0xb3, 31, 0), 'B3 1F 00');
});

test('describeSource covers note/cc/pitchbend and names the null decode', () => {
	assert.equal(fmt.describeSource({ ch: 2, kind: 'note', id: 60 }), 'ch 2 note 60');
	assert.equal(fmt.describeSource({ ch: 4, kind: 'cc', id: 31 }), 'ch 4 cc 31');
	assert.equal(fmt.describeSource({ ch: 1, kind: 'pitchbend', id: 0 }), 'ch 1 pitchbend');
	assert.equal(fmt.describeSource(null), 'undecoded');
});

test('formatLogTs renders seconds since page load and rejects bad input', () => {
	assert.equal(fmt.formatLogTs(2400), '2.4s');
	assert.equal(fmt.formatLogTs(0), '0.0s');
	assert.throws(() => fmt.formatLogTs(-1), RangeError);
	assert.throws(() => fmt.formatLogTs(NaN), RangeError);
});

// -------------------------------------------------------- label status logic

test('midiLabelStatus: amber while a request for MIDI turned on is pending', () => {
	assert.equal(fmt.midiLabelStatus('prompt', true, false, true), 'amber');
	assert.equal(fmt.midiLabelStatus('granted', true, true, true), 'amber');
	assert.equal(fmt.midiLabelStatus('denied', true, false, true), 'amber');
});

test('midiLabelStatus: turning MIDI off wins over a pending prompt (Codex P2 4131292220)', () => {
	// The prompt still open belongs to a superseded request (disableMidi bumped
	// the generation), so its late grant attaches nothing: MIDI is off.
	assert.equal(fmt.midiLabelStatus('prompt', true, false, false), 'grey');
	assert.equal(fmt.midiLabelStatus('granted', true, true, false), 'grey');
	assert.equal(fmt.midiLabelTitle('prompt', true, 0, 0, false), 'MIDI: off - turn it on in Settings');
	// Control: the same pending prompt with MIDI on is still amber and says so.
	assert.equal(fmt.midiLabelStatus('prompt', true, false, true), 'amber');
	assert.match(fmt.midiLabelTitle('prompt', true, 0, 0, true), /pending/);
});

test('midiLabelStatus: green when granted with a mapped device, red without', () => {
	assert.equal(fmt.midiLabelStatus('granted', false, true, true), 'green');
	// granted but no mapped device = access was granted then the controller
	// disconnected (or nothing recognised is plugged in) -> red X.
	assert.equal(fmt.midiLabelStatus('granted', false, false, true), 'red');
	assert.equal(fmt.midiLabelStatus('prompt', false, false, true), 'grey');
	assert.equal(fmt.midiLabelStatus('denied', false, false, true), 'grey');
	assert.equal(fmt.midiLabelStatus('unsupported', false, false, true), 'grey');
});

test('midiLabelStatus: granted but turned off is gray, not a lost device (Codex P2, PR #3896)', () => {
	// releaseMidiInputs() keeps the grant and empties the device list.
	assert.equal(fmt.midiLabelStatus('granted', false, false, false), 'grey');
	assert.equal(fmt.midiLabelStatus('granted', false, true, false), 'grey');
	// Control: the same empty list with MIDI on is still a lost device.
	assert.equal(fmt.midiLabelStatus('granted', false, false, true), 'red');
	// Off wins even over a request still in flight.
	assert.equal(fmt.midiLabelStatus('granted', true, false, false), 'grey');
});

test('midiLabelGlyph: tick for green, cross for red, none otherwise', () => {
	assert.equal(fmt.midiLabelGlyph('green'), 'tick');
	assert.equal(fmt.midiLabelGlyph('red'), 'cross');
	assert.equal(fmt.midiLabelGlyph('grey'), 'none');
	assert.equal(fmt.midiLabelGlyph('amber'), 'none');
});

test('midiLabelTitle: granted with zero mapped devices reads as disconnected', () => {
	assert.match(fmt.midiLabelTitle('granted', false, 0, 0, true), /granted but no mapped controller/);
	assert.match(fmt.midiLabelTitle('granted', false, 0, 1, true), /granted but no mapped controller/);
});

test('midiLabelTitle: granted but turned off says so and points at Settings', () => {
	const off = fmt.midiLabelTitle('granted', false, 0, 0, false);
	assert.match(off, /off - turn it on in Settings/);
	assert.doesNotMatch(off, /reconnect/);
	// Control: the other permission states read the same whatever the choice.
	assert.match(fmt.midiLabelTitle('denied', false, 0, 0, false), /denied/);
	assert.match(fmt.midiLabelTitle('prompt', false, 0, 0, false), /request access/);
});

test('midiLabelTitle names every permission state', () => {
	assert.match(fmt.midiLabelTitle('unsupported', false, 0, 0, true), /not supported/);
	assert.match(fmt.midiLabelTitle('denied', false, 0, 0, true), /denied/);
	assert.match(fmt.midiLabelTitle('prompt', false, 0, 0, true), /request access/);
	assert.match(fmt.midiLabelTitle('granted', false, 1, 2, true), /1 mapped \/ 2 connected/);
	assert.match(fmt.midiLabelTitle('granted', true, 0, 0, true), /pending/);
});

// ---------------------------------------------------- decoded trace labels

test('friendlyLabel turns action types into human labels', () => {
	assert.equal(fmt.friendlyLabel({ type: 'deck_play_toggle', deck: 1 }), 'Play (deck 1)');
	assert.equal(fmt.friendlyLabel({ type: 'deck_cue', deck: 2 }), 'Cue (deck 2)');
	assert.equal(fmt.friendlyLabel({ type: 'deck_hot_cue', deck: 3, slot: 'D' }), 'Hot cue D (deck 3)');
	assert.equal(fmt.friendlyLabel({ type: 'deck_beat_loop', deck: 1, beats: 4 }), 'Beat loop 4 (deck 1)');
	assert.equal(fmt.friendlyLabel({ type: 'deck_loop_exit', deck: 2 }), 'Loop exit (deck 2)');
	assert.equal(fmt.friendlyLabel({ type: 'mixer_channel', deck: 1, target: 'trim' }), 'Trim (deck 1)');
	assert.equal(fmt.friendlyLabel({ type: 'mixer_channel', deck: 4, target: 'fader' }), 'Channel fader (deck 4)');
	assert.equal(
		fmt.friendlyLabel({ type: 'mixer_channel', deck: 2, target: 'eq', band: 'low' }),
		'EQ low (deck 2)'
	);
	assert.equal(fmt.friendlyLabel({ type: 'mixer_global', target: 'crossfader' }), 'Crossfader');
	assert.equal(fmt.friendlyLabel({ type: 'mixer_global', target: 'master' }), 'Master level');
	assert.equal(fmt.friendlyLabel({ type: 'deck_pitch', deck: 2, lsbOffset: 32 }), 'Tempo (deck 2)');
	assert.equal(fmt.friendlyLabel({ type: 'browse_encoder' }), 'Browse');
	assert.equal(fmt.friendlyLabel({ type: 'browse_load', deck: 1 }), 'Load (deck 1)');
	assert.equal(fmt.friendlyLabel({ type: 'shift_modifier' }), 'Shift');
	assert.equal(fmt.friendlyLabel({ type: 'channel_cue', deck: 1 }), 'Cue / PFL (deck 1)');
	assert.equal(fmt.friendlyLabel({ type: 'headphone_mix' }), 'Headphones mix');
	assert.equal(fmt.friendlyLabel({ type: 'headphone_level' }), 'Headphones level');
	assert.equal(fmt.friendlyLabel({ type: 'master_cue', mode: 'latch' }), 'Master cue');
});

test('bestGuessHint names a documented-but-unbound control, else null', () => {
	// FLX10 [PDF] D14 LOOP IN deck 1 = note 0x10 on ch 1 - documented, unbound.
	assert.equal(
		fmt.bestGuessHint(flx10.FLX10_MAP, { ch: 1, kind: 'note', id: 0x10 }),
		'LOOP IN (deck 1)'
	);
	assert.equal(
		fmt.bestGuessHint(flx10.FLX10_MAP, { ch: 3, kind: 'note', id: 0x11 }),
		'LOOP OUT (deck 3)'
	);
	// A bound source (play, ch 1 note 0x0B) is not in the hint table.
	assert.equal(fmt.bestGuessHint(flx10.FLX10_MAP, { ch: 1, kind: 'note', id: 0x0b }), null);
	// Null map / null source / no match -> null (never throws).
	assert.equal(fmt.bestGuessHint(null, { ch: 1, kind: 'note', id: 0x10 }), null);
	assert.equal(fmt.bestGuessHint(flx10.FLX10_MAP, null), null);
});

test('traceLabel prefers action, then hint, then the raw note', () => {
	assert.equal(fmt.traceLabel('deck_play_toggle', { type: 'deck_play_toggle', deck: 1 }, null), 'Play (deck 1)');
	assert.equal(fmt.traceLabel('unmapped source', null, 'LOOP IN (deck 1)'), 'likely: LOOP IN (deck 1)');
	assert.equal(fmt.traceLabel('unmapped source', null, null), 'unmapped source');
	assert.equal(fmt.traceLabel('unmapped source', undefined, null), 'unmapped source');
});

// ------------------------------------------------------------- midi-ui-state

test('toggleMidiPanel flips and restores panelOpen', () => {
	const initial = uiState.midiUi.panelOpen;
	uiState.toggleMidiPanel();
	assert.equal(uiState.midiUi.panelOpen, !initial);
	uiState.toggleMidiPanel();
	assert.equal(uiState.midiUi.panelOpen, initial);
});

test('requestMidiAccess on a WebMIDI-less runtime fails LOUDLY into lastError', async () => {
	// Node has no navigator.requestMIDIAccess: initMidi throws 'unsupported'.
	await uiState.requestMidiAccess();
	assert.equal(uiState.midiUi.requestPending, false);
	assert.notEqual(uiState.midiUi.lastError, null);
	assert.match(uiState.midiUi.lastError, /not supported/i);
	assert.equal(webmidi.midiState.permission, 'unsupported');
	// Failure must release the action handler and meter interval: a second
	// request is a fresh failure, not "attachMidiGlue: already attached".
	await uiState.requestMidiAccess();
	assert.match(uiState.midiUi.lastError, /not supported/i);
});

test('requestMidiAccess throws on a concurrent second call', async () => {
	uiState.midiUi.requestPending = true; // simulate in-flight prompt
	await assert.rejects(() => uiState.requestMidiAccess(), /already pending/);
	uiState.midiUi.requestPending = false;
});

// ------------------------------------------------------ learn-log pop-out UI

test('log pop-out open/close/minimize toggles are self-consistent', () => {
	assert.equal(uiState.midiUi.logPopoutOpen, false);
	uiState.openLogPopout();
	assert.equal(uiState.midiUi.logPopoutOpen, true);
	assert.equal(uiState.midiUi.logPopoutMinimized, false); // open is always un-minimized
	uiState.toggleLogPopoutMinimized();
	assert.equal(uiState.midiUi.logPopoutMinimized, true);
	uiState.openLogPopout(); // re-open clears the minimized state
	assert.equal(uiState.midiUi.logPopoutMinimized, false);
	uiState.closeLogPopout();
	assert.equal(uiState.midiUi.logPopoutOpen, false);
});

// --------------------------------------------------- enabled-choice persistence

test('midiEnabledPersisted reflects the localStorage flag', () => {
	_installLocalStorage();
	assert.equal(uiState.midiEnabledPersisted(), false);
	globalThis.localStorage.setItem(enabledChoice.MIDI_ENABLED_KEY, '1');
	assert.equal(uiState.midiEnabledPersisted(), true);
	globalThis.localStorage.removeItem(enabledChoice.MIDI_ENABLED_KEY);
	assert.equal(uiState.midiEnabledPersisted(), false);
	_uninstallLocalStorage();
});

test('midiEnabledPersisted is false (no throw) when localStorage is absent', () => {
	_uninstallLocalStorage();
	assert.equal(uiState.midiEnabledPersisted(), false);
});

test('a failed requestMidiAccess clears the persisted enabled flag (no reload nag)', async () => {
	// Simulate "user enabled MIDI before", then a reload where access fails
	// (Node has no WebMIDI): the choice must be forgotten so we do not re-nag.
	_installLocalStorage({ [enabledChoice.MIDI_ENABLED_KEY]: '1' });
	assert.equal(uiState.midiEnabledPersisted(), true);
	await uiState.requestMidiAccess();
	assert.notEqual(uiState.midiUi.lastError, null); // failed loudly
	assert.equal(uiState.midiEnabledPersisted(), false); // and forgot the choice
	_uninstallLocalStorage();
});

test('maybeAutoEnableMidi is a no-op when the user never opted in', async () => {
	_installLocalStorage(); // key absent
	uiState.midiUi.lastError = null;
	await uiState.maybeAutoEnableMidi();
	assert.equal(uiState.midiUi.lastError, null); // never touched the request path
	assert.equal(uiState.midiUi.requestPending, false);
	_uninstallLocalStorage();
});

test('maybeAutoEnableMidi re-runs the request when the choice was persisted', async () => {
	// Persisted opt-in -> auto path calls requestMidiAccess, which fails loudly
	// in Node (no WebMIDI) and clears the flag: proves the wire actually fired.
	_installLocalStorage({ [enabledChoice.MIDI_ENABLED_KEY]: '1' });
	uiState.midiUi.lastError = null;
	await uiState.maybeAutoEnableMidi();
	assert.notEqual(uiState.midiUi.lastError, null);
	assert.equal(uiState.midiEnabledPersisted(), false);
	_uninstallLocalStorage();
});

// requirement: CHROME-08
test('MidiPanel supports expanded 70vw and floating width modes', async () => {
	const { readFileSync } = await import('node:fs');
	const { fileURLToPath } = await import('node:url');
	const panel = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/MidiPanel.svelte', import.meta.url)),
		'utf8'
	);
	assert.match(panel, /width:\s*70vw/);
	assert.match(panel, /data-width-mode=\{midiUi\.widthMode\}/);
	assert.match(panel, /toggleMidiPanelExpanded/);
	assert.match(panel, /floatMidiPanel/);
});
