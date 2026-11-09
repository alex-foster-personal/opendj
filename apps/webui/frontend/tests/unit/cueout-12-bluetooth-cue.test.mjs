// requirement: CUEOUT-12
// [if] the only leftover output after I/O unlock is a WH-1000XM5 [then] cueOutputAutoSelectCandidate returns that id
// [if] those Bluetooth headphones lose battery or power off [then] pinnedSinkReapplyPlan.applyMaster is false and the room stays two_outputs master-only
// [if] those Bluetooth headphones power back on and the remembered cue id is listed [then] restoreCue is true and applyMaster is still false
// [if] label unlock getUserMedia runs with Bluetooth attached [then] unlockAudioInputConstraints uses the built-in mic, never the headset HFP mic
// [if] a Bluetooth headphone is selected as HEADPHONE CUE [then] calibrateButtonEnabled is true and headphones.ts still has no first-select auto-chirp
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));

let headphones;
let cueAlignPolicy;

before(async () => {
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	cueAlignPolicy = await loadTypeScriptModule('src/lib/player/cue-align-policy.ts');
});

const SPEAKERS = { id: 'speakers', label: 'MacBook Pro Speakers' };
const SYSTEM_DEFAULT = { id: 'default', label: 'System default output' };

function autoSelect(outputs, selectedCueId = null) {
	return headphones.cueOutputAutoSelectCandidate(outputs, selectedCueId);
}

test('auto-select picks sole leftover WH-1000XM5 without Bluetooth token', () => {
	assert.equal(autoSelect([SPEAKERS, { id: 'bt', label: 'WH-1000XM5' }]), 'bt');
});

test('auto-select picks sole leftover Steves over-ears', () => {
	assert.equal(
		autoSelect([SPEAKERS, { id: 'over-ears', label: "a friend's over-ears" }]),
		'over-ears'
	);
});

test('auto-select picks named Bluetooth token label', () => {
	assert.equal(
		autoSelect([SPEAKERS, { id: 'bt', label: 'WH-1000XM5 (Bluetooth)' }]),
		'bt'
	);
});

test('auto-select picks sole AirPods Pro', () => {
	assert.equal(autoSelect([SPEAKERS, { id: 'airpods', label: 'AirPods Pro' }]), 'airpods');
});

test('auto-select returns null when two named headphone outputs', () => {
	assert.equal(
		autoSelect([
			SPEAKERS,
			{ id: 'bt', label: 'WH-1000XM5 (Bluetooth)' },
			{ id: 'hp2', label: 'Studio Headphones' }
		]),
		null
	);
});

test('auto-select returns null when two leftover outputs', () => {
	assert.equal(
		autoSelect([
			SPEAKERS,
			{ id: 'bt', label: 'WH-1000XM5' },
			{ id: 'over-ears', label: "a friend's over-ears" }
		]),
		null
	);
});

test('auto-select skips External Headphones jack', () => {
	assert.equal(autoSelect([SPEAKERS, { id: 'hp', label: 'External Headphones' }]), null);
});

test('auto-select returns null for speakers only', () => {
	assert.equal(autoSelect([SPEAKERS]), null);
});

test('auto-select picks bt when system default and speakers are ineligible', () => {
	assert.equal(
		autoSelect([SYSTEM_DEFAULT, SPEAKERS, { id: 'bt', label: 'WH-1000XM5' }]),
		'bt'
	);
});

test('auto-select returns null when cue already selected', () => {
	assert.equal(autoSelect([{ id: 'bt', label: 'WH-1000XM5' }], 'bt'), null);
});

test('auto-select returns null for empty outputs', () => {
	assert.equal(autoSelect([]), null);
});

test('auto-select documents Scarlett sole-leftover overshoot', () => {
	assert.equal(autoSelect([SPEAKERS, { id: 'iface', label: 'Scarlett 2i2' }]), 'iface');
});

test('auto-select rejects empty device id', () => {
	assert.equal(autoSelect([SPEAKERS, { id: '', label: 'WH-1000XM5' }]), null);
});

const SPEAKERS_ONLY = [
	{ id: 'lg', label: 'LG ULTRAWIDE' },
	{ id: 'speakers', label: 'MacBook Pro Speakers' }
];

const WITH_BT = [
	{ id: 'lg', label: 'LG ULTRAWIDE' },
	{ id: 'hp', label: 'External Headphones' },
	{ id: 'speakers', label: 'MacBook Pro Speakers' },
	{ id: 'bt', label: 'WH-1000XM5' }
];

test('cue battery-die is the same vanish as unplug: MASTER is not re-set', () => {
	const plan = headphones.pinnedSinkReapplyPlan({
		previousMasterId: 'speakers',
		nextMasterId: 'speakers',
		previousCueId: 'bt',
		nextCueId: null,
		rememberedCueId: 'bt',
		cueClearedByOperator: false,
		outputs: SPEAKERS_ONLY
	});
	assert.equal(plan.applyMaster, false);
	assert.equal(plan.clearCue, true);
	assert.equal(plan.restoreCue, false);
	assert.equal(plan.keepTwoOutputs, true);
	assert.deepEqual(headphones.practiceMainGains('two_outputs', null, 0), {
		cue: 0,
		master: 1
	});
});

test('cue power-back-on restores CUE and never re-sets MASTER', () => {
	const plan = headphones.pinnedSinkReapplyPlan({
		previousMasterId: 'speakers',
		nextMasterId: 'speakers',
		previousCueId: null,
		nextCueId: null,
		rememberedCueId: 'bt',
		cueClearedByOperator: false,
		outputs: WITH_BT
	});
	assert.deepEqual(plan, {
		applyMaster: false,
		applyCue: true,
		clearCue: false,
		restoreCue: true,
		applyBooth: false,
		clearBooth: false,
		restoreBooth: false,
		keepTwoOutputs: true
	});
});

test('label unlock prefers built-in mic over Bluetooth HFP', () => {
	const devices = [
		{ kind: 'audioinput', deviceId: 'hfp', label: 'WH-1000XM5 Hands-Free' },
		{ kind: 'audioinput', deviceId: 'mic', label: 'MacBook Pro Microphone' },
		{ kind: 'audiooutput', deviceId: 'bt', label: 'WH-1000XM5' }
	];
	assert.equal(headphones.preferredAudioInputDeviceId(devices), 'mic');
	assert.deepEqual(headphones.unlockAudioInputConstraints(devices, null), {
		deviceId: { ideal: 'mic' },
		echoCancellation: false,
		noiseSuppression: false,
		autoGainControl: false
	});
});

test('CALIBRATE is enabled for Bluetooth cue ids and disabled without a cue', () => {
	assert.equal(
		cueAlignPolicy.calibrateButtonEnabled({
			output_mode: 'two_outputs',
			selected_output_device_id: 'bt'
		}),
		true
	);
	assert.equal(
		cueAlignPolicy.calibrateButtonEnabled({
			output_mode: 'two_outputs',
			selected_output_device_id: 'over-ears'
		}),
		true
	);
	assert.equal(
		cueAlignPolicy.calibrateButtonEnabled({
			output_mode: 'two_outputs',
			selected_output_device_id: null
		}),
		false
	);
});

test('headphones.ts keeps P0 master guard and auto-select wiring without first-select auto-chirp', () => {
	const source = readFileSync(`${FRONTEND}/src/lib/player/headphones.ts`, 'utf8');
	assert.match(source, /if \(plan\.applyMaster\)/);
	assert.match(source, /cueOutputAutoSelectCandidate/);
	assert.match(source, /_autoSelectSoleBluetoothCue[\s\S]*cueOutputAutoSelectCandidate/);
	assert.doesNotMatch(source, /shouldCalibrateCueLatency|_maybeCalibrateCueLatency|_calibratedCueIds/);
});
