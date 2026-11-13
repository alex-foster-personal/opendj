// requirement: CTRL-10071
// Guided setup for an unmapped controller (#10071): the state module walks the
// fixed prompt order, records one binding per step and finishes into an
// installed-map document the runtime accepts.
//
// Regression lines (single-line format per CLAUDE.md):
//   if the prompt order is not deck 1 then deck 2 transport, mixer, globals, load, shift then broken
//   if a step's action type is not in the MidiAction union then broken
//   if a message from another port arms a step then broken
//   if a colliding binding is recorded instead of refused with both labels then broken
//   if a finished two-deck document fails validateDeviceMap then broken
//   if any binding lacks learned provenance then broken

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const TYPES_TS = new URL('../../src/lib/rb/midi/midi-types.ts', import.meta.url);

const PORT = { deviceId: 'dev-new', name: 'Acme DJ-2 (MIDI) 1', manufacturer: 'Acme' };
const STARTED = '2026-11-13T07:00:00.000Z';

let vite;
let webmidi;
let wizard;
let installed;

before(async () => {
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true, watch: null },
		plugins: [svelte()],
		resolve: {
			alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') },
			conditions: ['browser']
		}
	});
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
	installed = await vite.ssrLoadModule('/src/lib/rb/midi/installed-maps.ts');
	wizard = await vite.ssrLoadModule('/src/lib/components/rb/midi/midi-learn-wizard.ts');
});

after(async () => {
	await vite?.close();
});

/** Build a learn-log entry from a recorded byte triple. */
function entry(bytes, deviceId = PORT.deviceId) {
	const [status, data1, data2] = bytes;
	const hi = status & 0xf0;
	const ch = (status & 0x0f) + 1;
	const kind = hi === 0xb0 ? 'cc' : hi === 0xe0 ? 'pitchbend' : 'note';
	return {
		ts: 0,
		deviceId,
		deviceName: PORT.name,
		status,
		data1,
		data2,
		decoded: { ch, kind, id: kind === 'pitchbend' ? 0 : data1 },
		mapped: false,
		note: 'unmapped'
	};
}

/** One distinct recorded triple per step: notes for buttons, CCs for continuous controls. */
function recordedTriple(step, i) {
	const continuous = [
		'deck_pitch',
		'deck_jog',
		'mixer_channel',
		'mixer_global',
		'headphone_mix',
		'headphone_level',
		'browse_encoder'
	];
	return continuous.includes(step.action.type) ? [0xb0, i, 64] : [0x90, i, 127];
}

test('prompt order is fixed and every entry is a MidiAction type', () => {
	const labels = wizard.GUIDED_SETUP_STEPS.map((s) => s.label);
	assert.deepEqual(labels.slice(0, 11), [
		'Deck 1 play',
		'Deck 1 cue',
		'Deck 1 sync',
		'Deck 1 hot cue A',
		'Deck 1 hot cue B',
		'Deck 1 hot cue C',
		'Deck 1 hot cue D',
		'Deck 1 auto loop',
		'Deck 1 loop exit',
		'Deck 1 pitch fader',
		'Deck 1 jog wheel'
	]);
	assert.equal(labels[11], 'Deck 2 play');
	assert.deepEqual(labels.slice(22, 29), [
		'Channel 1 trim',
		'Channel 1 EQ high',
		'Channel 1 EQ mid',
		'Channel 1 EQ low',
		'Channel 1 filter',
		'Channel 1 fader',
		'Channel 1 cue'
	]);
	assert.deepEqual(labels.slice(36), [
		'Crossfader',
		'Master level',
		'Headphone mix',
		'Headphone level',
		'Browse encoder',
		'Load deck 1',
		'Load deck 2',
		'Shift'
	]);
	const src = readFileSync(TYPES_TS, 'utf8');
	const union = new Set([...src.matchAll(/^\ttype: '([a-z_]+)';$/gm)].map((m) => m[1]));
	for (const step of wizard.GUIDED_SETUP_STEPS) assert.ok(union.has(step.action.type), step.label);
});

test('a two-deck walk finishes into a document validateDeviceMap accepts', () => {
	const s = wizard.wizardStart(PORT, STARTED);
	wizard.GUIDED_SETUP_STEPS.forEach((step, i) => {
		assert.equal(wizard.wizardState(s).step.label, step.label);
		assert.equal(wizard.wizardCapture(s, entry(recordedTriple(step, i))), true);
		assert.deepEqual(wizard.wizardState(s).capture.bytes, recordedTriple(step, i));
		assert.equal(wizard.wizardConfirm(s), true);
	});
	assert.equal(wizard.wizardState(s).step, null);
	const doc = wizard.wizardFinish(s);
	assert.equal(doc.id, 'acme-dj-2-midi-1');
	assert.equal(doc.vendor, 'Acme');
	assert.equal(doc.model, PORT.name);
	assert.ok(new RegExp(doc.nameMatch).test(PORT.name));
	assert.equal(doc.bindings.length, wizard.GUIDED_SETUP_STEPS.length);
	for (const b of doc.bindings) {
		assert.deepEqual(b.provenance, {
			tier: 'learned',
			cite: `guided setup ${STARTED}, port ${PORT.name}`,
			verified: true
		});
	}
	const map = installed.deviceMapFromDoc(doc);
	assert.doesNotThrow(() => webmidi.validateDeviceMap(map));
	assert.deepEqual(installed.validateInstalledMapBatch([doc]).length, 1);
});

test('only the wizard device arms a step; Retry re-arms and Skip leaves it unbound', () => {
	const s = wizard.wizardStart(PORT, STARTED);
	assert.equal(wizard.wizardCapture(s, entry([0x90, 1, 127], 'other-port')), false);
	assert.equal(wizard.wizardState(s).capture, null);
	assert.equal(wizard.wizardCapture(s, entry([0x90, 1, 127])), true);
	wizard.wizardRetry(s);
	assert.equal(wizard.wizardState(s).capture, null);
	assert.equal(wizard.wizardCapture(s, entry([0x90, 2, 127])), true);
	assert.equal(wizard.wizardConfirm(s), true);
	assert.equal(wizard.wizardSkip(s), true);
	const st = wizard.wizardState(s);
	assert.deepEqual(st.skipped, [1]);
	assert.equal(st.step.label, 'Deck 1 sync');
	const doc = wizard.wizardFinish(s);
	assert.deepEqual(
		doc.bindings.map((b) => b.action.type),
		['deck_play_toggle']
	);
});

test('a colliding capture is refused naming both labels', () => {
	const s = wizard.wizardStart(PORT, STARTED);
	wizard.wizardCapture(s, entry([0x90, 5, 127]));
	wizard.wizardConfirm(s);
	assert.equal(wizard.wizardCapture(s, entry([0x90, 5, 127])), false);
	const st = wizard.wizardState(s);
	assert.equal(st.capture, null);
	assert.match(st.refused, /Deck 1 cue/);
	assert.match(st.refused, /Deck 1 play/);
	assert.equal(st.records.length, 1);
});

test('finishing with nothing recorded throws', () => {
	const s = wizard.wizardStart(PORT, STARTED);
	assert.throws(() => wizard.wizardFinish(s), /nothing to save/);
});
