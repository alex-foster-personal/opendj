// requirement: PGMIX-7692c
// Calibration needs an explicit microphone pick; AUDIO IN shows it at once
// (Preview 7fcc34f891, pin 3cfdec80ba0a).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';

const modal = readFrontendSource('src/lib/components/rb/mixer/CueAlignModal.svelte');
const cluster = readFrontendSource('src/lib/components/rb/mixer/HeadphoneCluster.svelte');
const copy = readFrontendSource('src/lib/components/rb/mixer/headphone-io-copy.ts');

test('the modal starts only once a listed microphone is picked', () => {
	assert.match(
		modal,
		/const micReady = \$derived\(\s*calibrationMicId\.trim\(\) !== '' && headphones\.inputs\.some\(\(input\) => input\.id === calibrationMicId\)\s*\);/
	);
	assert.match(modal, /disabled=\{cueAlignmentRunning\(\) \|\| !micReady\}/);
	assert.match(modal, /if \(!micReady\) \{\s*startError = 'Choose the microphone that will hear the chirps before starting\.';\s*return;\s*\}/);
	// The pick is flushed through the headphone queue before capture opens.
	const flush = modal.indexOf("type: 'headphone_input_select'");
	const capture = modal.indexOf('await startCueAlignment({ interactive: true })');
	assert.ok(flush > 0 && capture > flush, 'headphone_input_select runs before the capture starts');
	assert.match(modal, /<option value="" disabled=\{calibrationMicId !== ''\}>Choose a microphone<\/option>/);
});

test('the preferred input does not count as a pick', () => {
	// The calibration mic starts empty, not from selected_input_device_id.
	assert.match(cluster, /let calibrationMicId = \$state\(''\);/);
	assert.doesNotMatch(cluster, /calibrationMicId = headphoneState\.selected_input_device_id/);
	assert.match(cluster, /calibrationMicId=\{calibrationMicId\}\s*onPickMic=\{chooseCalibrationMic\}/);
});

test('AUDIO IN shows a pick before the command lands, then mirrors what the router holds', () => {
	assert.match(cluster, /const audioInValue = \$derived\(audioInPendingId \?\? headphoneState\.selected_input_device_id \?\? ''\);/);
	assert.match(cluster, /aria-label="audio input device"\s*value=\{audioInValue\}/);
	assert.match(
		cluster,
		/pickThenMirror\(event\.currentTarget, chooseCalibrationMic, \(\) => headphoneState\.selected_input_device_id\)/
	);
	assert.match(cluster, /if \(audioInPendingId === deviceId\) audioInPendingId = null;/);
});

test('copy names the chosen microphone, not the built-in one', () => {
	assert.doesNotMatch(copy, /built-in mic/);
	assert.match(copy, /The microphone CALIBRATE listens with\./);
	assert.doesNotMatch(modal, /the built-in microphone times both/);
});
