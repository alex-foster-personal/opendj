// Unit tests for the MIDI panel unit (midi-format pure helpers +
// midi-ui-state rune module). Same vite bootstrap as midi-core.test.mjs:
// rune modules need the svelte plugin + $lib alias.
//
// Regression lines (single-line format per CLAUDE.md):
//   if hexByte(0x90) isn't '90' or hexByte(300) doesn't throw then broken
//   if formatBytes(0x90,60,127) isn't '90 3C 7F' then broken
//   if describeSource(null) doesn't say 'undecoded' then broken
//   if midiLabelStatus(granted, no mapped device) is green then broken
//   if midiLabelStatus with requestPending isn't amber then broken
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
let webmidi; // webmidi.svelte.ts (for permission state assertions)

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
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
});

after(async () => {
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

test('midiLabelStatus: amber while a request is pending, above all else', () => {
	assert.equal(fmt.midiLabelStatus('prompt', true, false), 'amber');
	assert.equal(fmt.midiLabelStatus('granted', true, true), 'amber');
});

test('midiLabelStatus: green ONLY when granted with a mapped device', () => {
	assert.equal(fmt.midiLabelStatus('granted', false, true), 'green');
	assert.equal(fmt.midiLabelStatus('granted', false, false), 'grey'); // unmapped-only
	assert.equal(fmt.midiLabelStatus('prompt', false, false), 'grey');
	assert.equal(fmt.midiLabelStatus('denied', false, false), 'grey');
	assert.equal(fmt.midiLabelStatus('unsupported', false, false), 'grey');
});

test('midiLabelTitle names every permission state', () => {
	assert.match(fmt.midiLabelTitle('unsupported', false, 0, 0), /not supported/);
	assert.match(fmt.midiLabelTitle('denied', false, 0, 0), /denied/);
	assert.match(fmt.midiLabelTitle('prompt', false, 0, 0), /request access/);
	assert.match(fmt.midiLabelTitle('granted', false, 1, 2), /1 mapped \/ 2 connected/);
	assert.match(fmt.midiLabelTitle('granted', true, 0, 0), /pending/);
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
});

test('requestMidiAccess throws on a concurrent second call', async () => {
	uiState.midiUi.requestPending = true; // simulate in-flight prompt
	await assert.rejects(() => uiState.requestMidiAccess(), /already pending/);
	uiState.midiUi.requestPending = false;
});
