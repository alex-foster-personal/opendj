// requirement: BTHFP-8319
// [if] Bluetooth headphones dropped to call mode [then] the banner names them and says how to fix it
// [if] the headphones' mic is the system input [then] the banner warns before the flip
// [if] the engine could not look, or all is clear [then] no banner at all
// [if] the operator dismissed one state [then] a different state still shows
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
const device = (name, transport, rate) => ({ device_id: 1, name, transport, nominal_sample_rate_hz: rate });
const report = (verdict, output, input = device('USB MICROPHONE', 'usb', 48000)) => ({
	supported: verdict !== 'unsupported',
	verdict,
	output,
	input,
	error: null
});

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/bluetooth-headset-mode.svelte.ts');
});

test('call mode names the headphones and gives the Option-click and Settings fix', () => {
	const w = mod.headsetModeWarning(report('headset_mode', device('af-sony-fun', 'bluetooth', 16000)));
	assert.match(w.text, /^af-sony-fun switched to headset \(call\) mode/);
	assert.match(w.text, /Option-click the sound icon in the menu bar/);
	assert.match(w.text, /System Settings > Sound/);
	assert.match(w.title, /16000 Hz/);
});

test('mic selected warns before the flip and names the input on hover', () => {
	const sony = device('af-sony-fun', 'bluetooth', 44100);
	const w = mod.headsetModeWarning(report('mic_selected', sony, sony));
	assert.match(w.text, /microphone is the Mac's sound input/);
	assert.match(w.title, /System input: af-sony-fun/);
});

test('an unnamed device still reads as a sentence', () => {
	const w = mod.headsetModeWarning(report('headset_mode', device('', 'bluetooth', 8000)));
	assert.match(w.text, /^Your Bluetooth headphones switched/);
});

test('clear, unsupported and missing reports render nothing', () => {
	assert.equal(mod.headsetModeWarning(null), null);
	assert.equal(mod.headsetModeWarning(report('clear', device('af-sony-fun', 'bluetooth', 44100))), null);
	assert.equal(mod.headsetModeWarning(report('unsupported', null, null)), null);
	// A supported flag with no output is not trusted as a warning either.
	assert.equal(mod.headsetModeWarning({ ...report('headset_mode', null), supported: true }), null);
});

test('dismissal is keyed to the state, so a later flip shows again', () => {
	const sony = device('af-sony-fun', 'bluetooth', 44100);
	const selected = mod.headsetModeWarning(report('mic_selected', sony, sony));
	const flipped = mod.headsetModeWarning(report('headset_mode', device('af-sony-fun', 'bluetooth', 16000)));
	const other = mod.headsetModeWarning(report('mic_selected', device('AirPods', 'bluetooth', 48000)));
	assert.notEqual(selected.key, flipped.key);
	assert.notEqual(selected.key, other.key);
});

test('refresh turns an HTTP error into unsupported, never a stale warning', async () => {
	await mod.refreshBluetoothHeadsetMode(async () => ({
		ok: true,
		json: async () => report('headset_mode', device('af-sony-fun', 'bluetooth', 16000))
	}));
	assert.equal(mod.bluetoothHeadsetMode.report.verdict, 'headset_mode');
	await mod.refreshBluetoothHeadsetMode(async () => ({ ok: false, status: 503 }));
	assert.equal(mod.bluetoothHeadsetMode.report.verdict, 'unsupported');
	assert.equal(mod.headsetModeWarning(mod.bluetoothHeadsetMode.report), null);
});

test('a dismissal ends with its episode: clear then the same flip warns again', async () => {
	const flipped = report('headset_mode', device('af-sony-fun', 'bluetooth', 16000));
	const serve = (body) => async () => ({ ok: true, json: async () => body });
	await mod.refreshBluetoothHeadsetMode(serve(flipped));
	mod.dismissHeadsetModeWarning(mod.headsetModeWarning(flipped).key);
	await mod.refreshBluetoothHeadsetMode(serve(flipped));
	assert.notEqual(mod.bluetoothHeadsetMode.dismissedKey, null, 'same episode stays dismissed');
	await mod.refreshBluetoothHeadsetMode(async () => ({ ok: false, status: 503 }));
	assert.notEqual(mod.bluetoothHeadsetMode.dismissedKey, null, 'a failed read is not a recovery');
	await mod.refreshBluetoothHeadsetMode(serve(report('clear', device('af-sony-fun', 'bluetooth', 44100))));
	assert.equal(mod.bluetoothHeadsetMode.dismissedKey, null, 'recovery ends the dismissal');
	await mod.refreshBluetoothHeadsetMode(serve(flipped));
	assert.notEqual(mod.headsetModeWarning(mod.bluetoothHeadsetMode.report).key, mod.bluetoothHeadsetMode.dismissedKey);
});

test('the poll asks the configured daemon for the headset-mode route', async () => {
	const seen = [];
	await mod.refreshBluetoothHeadsetMode(async (url) => {
		seen.push(String(url));
		return { ok: false, status: 503 };
	});
	// Same-origin in tests (VITE_API_BASE unset), so the URL is exactly the route.
	assert.deepEqual(seen, ['/api/v1/audio-headset-mode']);
});
