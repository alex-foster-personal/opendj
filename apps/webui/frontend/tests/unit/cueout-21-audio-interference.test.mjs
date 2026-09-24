// requirement: CUEOUT-21 (ChirpSync round 8)
// [if] a known recorder is running [then] the modal warns before a run is wasted
// [if] the engine could not look [then] the modal says nothing, rather than an all-clear
// [if] nothing is running [then] no warning appears at all
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
const item = (key, label, kind = 'running_app') => ({
	key,
	label,
	kind,
	why: 'it listens to the microphone',
	matched: `/Applications/${label}.app`
});

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/audio-interference.svelte.ts');
});

test('one offender is named, with what to do about it', () => {
	const line = mod.audioInterferenceWarning({
		supported: true,
		detected: [item('fathom', 'Fathom')],
		error: null
	});
	assert.match(line, /Fathom is running/);
	assert.match(line, /Quit it before calibrating/);
});

test('several offenders read as a list rather than a repeated sentence', () => {
	const line = mod.audioInterferenceWarning({
		supported: true,
		detected: [item('fathom', 'Fathom'), item('loom', 'Loom'), item('zoom', 'Zoom')],
		error: null
	});
	assert.match(line, /Fathom, Loom and Zoom are running/);
});

test('an installed driver is not reported as a running app', () => {
	// P2 r4052499263. A Krisp HAL plug-in is in the audio stack with the Krisp
	// app closed, so "quit it" is both false about the state and useless as a
	// remedy: quitting something already closed cannot remove a plug-in.
	const line = mod.audioInterferenceWarning({
		supported: true,
		detected: [item('krisp', 'Krisp', 'audio_driver')],
		error: null
	});
	assert.doesNotMatch(line, /Krisp is running/,
		'if an installed plug-in is called "running" then the advisory states a false status - broken');
	assert.match(line, /Krisp is installed as an audio plug-in/,
		'if the line does not say what was actually found then the reader cannot act on it - broken');
	assert.match(line, /Disable or remove it in its own settings/,
		'if a plug-in finding says "quit it" then the remedy cannot work - broken');
});

test('an app and a driver together each get their own sentence and remedy', () => {
	const line = mod.audioInterferenceWarning({
		supported: true,
		detected: [item('fathom', 'Fathom'), item('krisp', 'Krisp', 'audio_driver')],
		error: null
	});
	assert.match(line, /Fathom is running/);
	assert.match(line, /Krisp is installed as an audio plug-in/);
	assert.match(line, /Quit the app, and disable or remove the plug-in/,
		'if a mixed report collapses to one remedy then one of the two findings is left unactionable - broken');
});

test('a clean machine produces no warning', () => {
	assert.equal(mod.audioInterferenceWarning({ supported: true, detected: [], error: null }), null);
});

test('an engine that could not look is silent, not an all-clear', () => {
	// The control that matters: rendering "nothing is interfering" off a failed
	// probe is exactly the false-clean this whole workstream keeps hitting.
	assert.equal(
		mod.audioInterferenceWarning({ supported: false, detected: [], error: 'no process list' }),
		null
	);
});

test('a missing report is silent', () => {
	assert.equal(mod.audioInterferenceWarning(null), null);
});

test('the warning never claims to know who holds the device', () => {
	const line = mod.audioInterferenceWarning({
		supported: true,
		detected: [item('fathom', 'Fathom')],
		error: null
	});
	for (const forbidden of ['is holding', 'has taken', 'is using your microphone']) {
		assert.ok(!line.toLowerCase().includes(forbidden), `must not claim: ${forbidden}`);
	}
});

test('a failed fetch is recorded as unsupported rather than throwing into the run', async () => {
	await mod.refreshAudioInterference(async () => {
		throw new Error('engine unreachable');
	});
	assert.equal(mod.audioInterference.report.supported, false);
	assert.match(mod.audioInterference.report.error, /engine unreachable/);
	assert.equal(mod.audioInterferenceWarning(mod.audioInterference.report), null);
});

test('a non-2xx answer is unsupported, not an empty all-clear', async () => {
	await mod.refreshAudioInterference(async () => ({ ok: false, status: 503 }));
	assert.equal(mod.audioInterference.report.supported, false);
	assert.match(mod.audioInterference.report.error, /503/);
});

test('a good answer is stored as given', async () => {
	await mod.refreshAudioInterference(async () => ({
		ok: true,
		json: async () => ({ supported: true, detected: [item('granola', 'Granola')], error: null })
	}));
	assert.equal(mod.audioInterference.report.detected[0].label, 'Granola');
	assert.match(mod.audioInterferenceWarning(mod.audioInterference.report), /Granola is running/);
});
