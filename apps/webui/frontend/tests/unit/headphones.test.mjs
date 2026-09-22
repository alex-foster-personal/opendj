// requirement: CUEOUT-06, IOPIN-08
// [if] headphoneMixGains(0.5) is called [then] both gains equal cos(pi/4) to 1e-9, and 0 and 1 return exact 1/0 and 0/1
// [if] a device id vanishes from a refresh while selected [then] reconcileHeadphoneOutputRefresh clears selection and active
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let headphones;
const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));

before(async () => {
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
});

test('headphoneMixAccent is cue orange at 0 and master accent at 1, never EQ red', () => {
	assert.equal(
		headphones.headphoneMixAccent(0),
		'color-mix(in srgb, var(--rb-orange) 100%, var(--rb-accent))'
	);
	assert.equal(
		headphones.headphoneMixAccent(1),
		'color-mix(in srgb, var(--rb-orange) 0%, var(--rb-accent))'
	);
	assert.equal(
		headphones.headphoneMixAccent(0.5),
		'color-mix(in srgb, var(--rb-orange) 50%, var(--rb-accent))'
	);
	assert.doesNotMatch(headphones.headphoneMixAccent(0.5), /red/i);
});

test('headphoneMixGains uses equal-power law at endpoints and center', () => {
	assert.deepEqual(headphones.headphoneMixGains(0), { cue: 1, master: 0 });
	assert.deepEqual(headphones.headphoneMixGains(1), { cue: 0, master: 1 });
	const half = headphones.headphoneMixGains(0.5);
	const expected = Math.cos(Math.PI / 4);
	assert.ok(Math.abs(half.cue - expected) < 1e-9, `cue ${half.cue} != cos(pi/4)`);
	assert.ok(Math.abs(half.master - expected) < 1e-9, `master ${half.master} != cos(pi/4)`);
});

test('mergeHeadphoneOutput appends or refreshes enumerated outputs', () => {
	const base = [{ id: 'built-in', label: 'Built-in' }];
	assert.deepEqual(
		headphones.mergeHeadphoneOutput(base, { deviceId: 'usb-hp', label: 'USB Headphones' }),
		[
			{ id: 'built-in', label: 'Built-in' },
			{ id: 'usb-hp', label: 'USB Headphones' }
		]
	);
	assert.deepEqual(
		headphones.mergeHeadphoneOutput(base, { deviceId: 'built-in', label: 'Built-in Output' }),
		[{ id: 'built-in', label: 'Built-in Output' }]
	);
});

test('reconcileHeadphoneOutputRefresh clears stale selection and active', () => {
	const outputs = [{ id: 'present', label: 'Present' }];
	assert.deepEqual(
		headphones.reconcileHeadphoneOutputRefresh(true, 'vanished', outputs),
		{ active: false, selected_output_device_id: null }
	);
	assert.deepEqual(
		headphones.reconcileHeadphoneOutputRefresh(true, 'present', outputs),
		{ active: true, selected_output_device_id: 'present' }
	);
	assert.deepEqual(
		headphones.reconcileHeadphoneOutputRefresh(false, null, outputs),
		{ active: false, selected_output_device_id: null }
	);
});

test('headphoneOwnershipIsCurrent requires matching generation and owned nodes', () => {
	assert.equal(headphones.headphoneOwnershipIsCurrent(3, 3, true), true);
	assert.equal(headphones.headphoneOwnershipIsCurrent(3, 4, true), false);
	assert.equal(headphones.headphoneOwnershipIsCurrent(3, 3, false), false);
});

test('headphoneAcquisitionKind uses the chooser only when selectAudioOutput exists', () => {
	assert.equal(
		headphones.headphoneAcquisitionKind({
			enumerateDevices: true,
			setSinkId: true,
			selectAudioOutput: true
		}),
		'chooser'
	);
	assert.equal(
		headphones.headphoneAcquisitionKind({
			enumerateDevices: true,
			setSinkId: true,
			selectAudioOutput: false
		}),
		'unlock_and_enumerate'
	);
	assert.equal(
		headphones.headphoneAcquisitionKind({
			enumerateDevices: true,
			setSinkId: false,
			selectAudioOutput: false
		}),
		'unsupported'
	);
	assert.throws(
		() => headphones.headphoneAcquisitionKind({ enumerateDevices: true, setSinkId: true }),
		/booleans/
	);
});

test('signal indicators clear rather than re-timestamping stale analyser data while the context clock is stopped', () => {
	const source = readFileSync(`${FRONTEND}/src/lib/player/headphones.ts`, 'utf8');
	assert.match(source, /context\.state !== 'running'/);
	assert.match(source, /contextTime <= lastContextTime/);
	assert.match(source, /_clearSignal\('master', 'inactive'\)/);
	assert.match(source, /level\.connect\(cueSignalAnalyser\)/);
});

test('preferredAudioInputDeviceId prefers a built-in mic over Bluetooth-looking inputs', () => {
	assert.equal(headphones.preferredAudioInputDeviceId([]), null);
	assert.equal(
		headphones.preferredAudioInputDeviceId([
			{ kind: 'audiooutput', deviceId: 'speakers', label: 'MacBook Pro Speakers' },
			{ kind: 'audioinput', deviceId: 'airpods-mic', label: 'AirPods Microphone' },
			{ kind: 'audioinput', deviceId: 'builtin-mic', label: 'MacBook Pro Microphone' }
		]),
		'builtin-mic'
	);
	assert.equal(
		headphones.preferredAudioInputDeviceId([
			{ kind: 'audioinput', deviceId: 'default', label: '' },
			{ kind: 'audioinput', deviceId: 'usb-mic', label: 'USB Condenser' }
		]),
		'usb-mic'
	);
});

test('assertHeadphoneOutputSelection rejects empty ids and unknown devices', () => {
	const outputs = [{ id: 'hp-1', label: 'HP 1' }];
	assert.doesNotThrow(() => headphones.assertHeadphoneOutputSelection('hp-1', outputs));
	assert.throws(
		() => headphones.assertHeadphoneOutputSelection('missing', outputs),
		/not an enumerated headphone output/
	);
	assert.throws(
		() => headphones.assertHeadphoneOutputSelection('   ', outputs),
		/non-empty string/
	);
});
