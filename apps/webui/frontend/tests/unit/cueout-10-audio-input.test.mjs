// requirement: CUEOUT-10
// [if] enumerated inputs include MacBook Pro Microphone and an AirPods / headphone mic [then] preferredAudioInputDeviceId returns the MacBook mic
// [if] the only named input looks like headphone/headset/handsfree [then] preferredAudioInputDeviceId returns null rather than that device
// [if] AUDIO IN is changed to a listed input [then] selected_input_device_id is that id and the next label-unlock getUserMedia constrains to it
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let headphones;

before(async () => {
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
});

test('inputLooksLikeHandsfree matches headset mics that would collapse Bluetooth to HFP', () => {
	assert.equal(headphones.inputLooksLikeHandsfree('AirPods Microphone'), true);
	assert.equal(headphones.inputLooksLikeHandsfree('External Headphones'), true);
	assert.equal(headphones.inputLooksLikeHandsfree('iPhone Headset'), true);
	assert.equal(headphones.inputLooksLikeHandsfree('Hands-Free AG Audio'), true);
	assert.equal(headphones.inputLooksLikeHandsfree('MacBook Pro Microphone'), false);
	assert.equal(headphones.inputLooksLikeHandsfree('MX Brio'), false);
});

test('preferredAudioInputDeviceId never returns a headphone or handsfree mic when a safe input exists', () => {
	assert.equal(
		headphones.preferredAudioInputDeviceId([
			{ kind: 'audioinput', deviceId: 'airpods-mic', label: 'AirPods Microphone' },
			{ kind: 'audioinput', deviceId: 'builtin-mic', label: 'MacBook Pro Microphone' }
		]),
		'builtin-mic'
	);
	assert.equal(
		headphones.preferredAudioInputDeviceId([
			{ kind: 'audioinput', deviceId: 'headset', label: 'External Headphones' },
			{ kind: 'audioinput', deviceId: 'usb-mic', label: 'USB Condenser' }
		]),
		'usb-mic'
	);
});

test('preferredAudioInputDeviceId returns null when every named input looks like handsfree', () => {
	assert.equal(
		headphones.preferredAudioInputDeviceId([
			{ kind: 'audioinput', deviceId: 'airpods-mic', label: 'AirPods Microphone' }
		]),
		null
	);
	assert.equal(headphones.preferredAudioInputDeviceId([]), null);
});

test('unlockAudioInputConstraints requires the selected input exactly, falls back to the safe default, never a handsfree mic', () => {
	const devices = [
		{ kind: 'audioinput', deviceId: 'airpods-mic', label: 'AirPods Microphone' },
		{ kind: 'audioinput', deviceId: 'builtin-mic', label: 'MacBook Pro Microphone' }
	];
	assert.deepEqual(headphones.unlockAudioInputConstraints(devices, null), {
		deviceId: { ideal: 'builtin-mic' },
		echoCancellation: false,
		noiseSuppression: false,
		autoGainControl: false
	});
	// A selected input is EXACT. Chromium treats `ideal` as a hint and hands
	// back the system default mic instead, which is how calibration listened
	// to a closed-lid built-in mic while the MX Brio sat selected.
	assert.deepEqual(headphones.unlockAudioInputConstraints(devices, 'builtin-mic'), {
		deviceId: { exact: 'builtin-mic' },
		echoCancellation: false,
		noiseSuppression: false,
		autoGainControl: false
	});
	assert.deepEqual(headphones.unlockAudioInputConstraints(devices, 'airpods-mic'), {
		deviceId: { ideal: 'builtin-mic' },
		echoCancellation: false,
		noiseSuppression: false,
		autoGainControl: false
	});
});
