// CUEOUT (passive AUDIO IN meter vs MAIN in the Mac app). Silver, Tue 3 Nov
// 2026: with the I/O panel open, its passive AUDIO IN meter held the MX Brio
// microphone open from 13:35:36; WebKit ran voice-processing IO with the page
// session in PlayAndRecord / VideoChat and MAIN (HYPERBOOM, the page's own
// output) went silent while the shell's CUE kept sounding.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import {
	MIC_WHILE_ROOM_PLAYS_REFUSAL,
	PASSIVE_INPUT_METER_OFF_NOTE,
	ROOM_PLAYING_RMS_FLOOR,
	micOpenRefusal,
	passiveInputMeterBlockedReason
} from '../../src/lib/player/io-device-test.ts';

test('if the Mac app webview may open the passive input meter then an open I/O panel silences MAIN', () => {
	assert.equal(passiveInputMeterBlockedReason(true), PASSIVE_INPUT_METER_OFF_NOTE);
	assert.match(PASSIVE_INPUT_METER_OFF_NOTE, /MAIN/);
	assert.match(PASSIVE_INPUT_METER_OFF_NOTE, /Test/);
});

test('if a browser tab loses its passive input meter then the Chrome/Electron panel regressed', () => {
	assert.equal(passiveInputMeterBlockedReason(false), null);
});

test('if a non-boolean is accepted then a missing shell flag hides the decision', () => {
	assert.throws(() => passiveInputMeterBlockedReason(undefined), TypeError);
	assert.throws(() => passiveInputMeterBlockedReason('true'), TypeError);
});

// Live state at 13:44Z: master bus rms 0.0365 (deck 2 playing) in the Mac app.
test('if the Mac app opens a microphone while MAIN carries the set then the Bluetooth MAIN can drop to SCO and go silent', () => {
	assert.equal(micOpenRefusal({ macAppWebview: true, masterRms: 0.036465689407801524 }), MIC_WHILE_ROOM_PLAYS_REFUSAL);
	assert.equal(micOpenRefusal({ macAppWebview: true, masterRms: ROOM_PLAYING_RMS_FLOOR }), MIC_WHILE_ROOM_PLAYS_REFUSAL);
});

test('if an idle Mac app or any browser is refused the microphone then the AUDIO IN Test and calibration regressed', () => {
	assert.equal(micOpenRefusal({ macAppWebview: true, masterRms: 0 }), null);
	assert.equal(micOpenRefusal({ macAppWebview: true, masterRms: null }), null);
	assert.equal(micOpenRefusal({ macAppWebview: false, masterRms: 0.5 }), null);
});

test('if a malformed level is accepted then a broken meter silently opens the mic', () => {
	assert.throws(() => micOpenRefusal({ macAppWebview: true, masterRms: Number.NaN }), TypeError);
	assert.throws(() => micOpenRefusal({ macAppWebview: 'yes', masterRms: 0 }), TypeError);
});

test('if startInputLevelWatch reaches getUserMedia before the refusal then the guard is bypassed', () => {
	const audio = readFileSync(new URL('../../src/lib/player/io-device-test-audio.ts', import.meta.url), 'utf8');
	const body = audio.slice(audio.indexOf('export async function startInputLevelWatch('));
	const gate = body.indexOf('assertMicOpenAllowedNow();');
	const open = body.indexOf('navigator.mediaDevices.getUserMedia(');
	assert.ok(gate > 0 && open > 0 && gate < open, 'refusal must come before the microphone opens');
	const helper = audio.slice(audio.indexOf('export function assertMicOpenAllowedNow('));
	assert.match(helper.slice(0, helper.indexOf('\n}\n')), /micOpenRefusal\(\{\s*macAppWebview: nativeCueSinkAvailable\(\)/);
});
