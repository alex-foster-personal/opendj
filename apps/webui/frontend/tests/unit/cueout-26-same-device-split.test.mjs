// requirement: CUEOUT-26
// [if] MAIN and CUE are one physical output (the same id, or the MacBook speakers and the occupied headphone jack) [then] dualSinkAssignment runs split cue on that output, [else stop]
// [if] MAIN and CUE are genuinely different devices [then] two-output routing is kept with no split, [else stop]
// [if] the shell says an output is muted by the jack [then] it is never auto-picked and an explicit MAIN pick of it is refused, [else stop]
// [if] the shell releases a MAIN pin [then] the page clears the pin, says why, and re-plans, [else stop]
// [if] split cue runs because of one shared output [then] the TopBar shows "Split cue: master L / cue R (same device ...)", [else stop]
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));
const HEADPHONES = `${FRONTEND}/src/lib/player/headphones.ts`;
const TOPBAR = `${FRONTEND}/src/lib/components/rb/TopBar.svelte`;
const BADGE = `${FRONTEND}/src/lib/components/rb/SameDeviceSplitBadge.svelte`;

let headphones;
let collision;
let sink;

before(async () => {
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	collision = await loadTypeScriptModule('src/lib/player/main-cue-collision.ts');
	sink = await loadTypeScriptModule('src/lib/player/cue-native-sink.ts');
});

const HP = 'native:BuiltInHeadphoneOutputDevice';
const SPEAKERS = 'native:BuiltInSpeakerDevice';
const LG = 'native:lg';
const BLACKHOLE = 'native:blackhole';

/** silver, Tue 6 Oct 2026 (build 10), as the Mac shell lists it: wired
 * headphones in the MacBook's own jack mute the speakers. */
const SILVER_JACK = sink.nativeOutputsAsHeadphoneOutputs([
	{ uid: 'BuiltInSpeakerDevice', name: 'MacBook Pro Speakers', channels: 2, transport: 'builtin', is_default: false, muted_by_jack: true, physical_uid: 'BuiltInHeadphoneOutputDevice' },
	{ uid: 'BuiltInHeadphoneOutputDevice', name: 'External Headphones', channels: 2, transport: 'builtin', is_default: true, muted_by_jack: false, physical_uid: 'BuiltInHeadphoneOutputDevice' },
	{ uid: 'lg', name: 'LG ULTRAWIDE', channels: 2, transport: 'display', is_default: false, muted_by_jack: false, physical_uid: 'lg' },
	{ uid: 'blackhole', name: 'BlackHole 2ch', channels: 2, transport: 'virtual', is_default: false, muted_by_jack: false, physical_uid: 'blackhole' }
]);

const splitOnHp = (autoPinnedMaster) => ({ masterId: HP, cueId: HP, autoPinnedMaster, splitSameDevice: true });

test('the built-in pair with the jack occupied resolves to split cue on the jack, whatever the room says', () => {
	// The build-10 sequence: cue picked on the jack while the jack was the default.
	assert.deepEqual(
		headphones.dualSinkAssignment({ outputs: SILVER_JACK, selectedCueId: HP, selectedMasterId: null, currentRoomId: HP }),
		splitOnHp(true)
	);
	// The CUEOUT-25 repair had pinned MAIN on the muted speakers: still one output.
	assert.deepEqual(
		headphones.dualSinkAssignment({ outputs: SILVER_JACK, selectedCueId: HP, selectedMasterId: SPEAKERS, currentRoomId: HP }),
		splitOnHp(true)
	);
	// The end state: the muted speakers are the macOS default.
	assert.deepEqual(
		headphones.dualSinkAssignment({ outputs: SILVER_JACK, selectedCueId: HP, selectedMasterId: null, currentRoomId: SPEAKERS }),
		splitOnHp(true)
	);
	// An identical id is one output too.
	assert.deepEqual(
		headphones.dualSinkAssignment({ outputs: SILVER_JACK, selectedCueId: HP, selectedMasterId: HP, currentRoomId: HP }),
		splitOnHp(false)
	);
});

test('control: genuinely different devices keep two outputs and never split', () => {
	// An explicit MAIN on the display is the operator's choice: kept.
	assert.deepEqual(
		headphones.dualSinkAssignment({ outputs: SILVER_JACK, selectedCueId: HP, selectedMasterId: LG, currentRoomId: HP }),
		{ masterId: LG, cueId: HP, autoPinnedMaster: false }
	);
	// A USB interface is a real room output: auto-picked, no split.
	const withUsb = [
		...SILVER_JACK,
		...sink.nativeOutputsAsHeadphoneOutputs([
			{ uid: 'scarlett', name: 'Scarlett 2i2', channels: 2, transport: 'usb', is_default: false, muted_by_jack: false, physical_uid: 'scarlett' }
		])
	];
	assert.deepEqual(
		headphones.dualSinkAssignment({ outputs: withUsb, selectedCueId: HP, selectedMasterId: null, currentRoomId: HP }),
		{ masterId: 'native:scarlett', cueId: HP, autoPinnedMaster: true }
	);
	// Bluetooth headphones do not mute the speakers: CUEOUT-09's speaker auto-pin stands.
	const bluetooth = sink.nativeOutputsAsHeadphoneOutputs([
		{ uid: 'BuiltInSpeakerDevice', name: 'MacBook Pro Speakers', channels: 2, transport: 'builtin', is_default: false, muted_by_jack: false, physical_uid: 'BuiltInSpeakerDevice' },
		{ uid: 'airpods', name: 'AirPods Pro', channels: 2, transport: 'bluetooth', is_default: true, muted_by_jack: false, physical_uid: 'airpods' }
	]);
	assert.deepEqual(
		headphones.dualSinkAssignment({ outputs: bluetooth, selectedCueId: 'native:airpods', selectedMasterId: null, currentRoomId: 'native:airpods' }),
		{ masterId: SPEAKERS, cueId: 'native:airpods', autoPinnedMaster: true }
	);
});

test('the auto-pick never chooses the jack-muted speakers, a virtual device or a display', () => {
	assert.equal(headphones.preferredMasterOutputDeviceId(SILVER_JACK, HP), null);
	// Control: a Chrome listing has no shell fields, so its speaker guess is unchanged.
	const chrome = [
		{ id: 'hp', label: 'External Headphones' },
		{ id: 'spk', label: 'MacBook Pro Speakers' }
	];
	assert.equal(headphones.preferredMasterOutputDeviceId(chrome, 'hp'), 'spk');
});

test('an explicit MAIN on the jack-muted speakers is refused before the live route is touched', () => {
	assert.throws(() => collision.assertMasterCanSound(SPEAKERS, SILVER_JACK), /muted while headphones are in the headphone jack/);
	assert.doesNotThrow(() => collision.assertMasterCanSound(HP, SILVER_JACK));
	assert.doesNotThrow(() => collision.assertMasterCanSound(LG, SILVER_JACK), 'an explicit display pick is the operator\'s call');
	const source = readFileSync(HEADPHONES, 'utf8');
	const body = source.slice(source.indexOf('export async function selectMasterOutput('));
	const guard = body.indexOf('assertMasterCanSound(');
	assert.ok(guard > 0 && guard < body.indexOf('try {'), 'the refusal runs before the try that fails the live MAIN route');
});

test('physical sameness follows the shell, and a browser listing compares ids', () => {
	assert.ok(collision.sameOutputDevice(SILVER_JACK, SPEAKERS, HP));
	assert.ok(!collision.sameOutputDevice(SILVER_JACK, LG, HP));
	assert.ok(!collision.sameOutputDevice([{ id: 'a', label: 'A' }, { id: 'b', label: 'B' }], 'a', 'b'));
});

test('a MAIN left on the muted speakers raises the cannot_sound banner fault in any mode', () => {
	const state = {
		output_mode: 'practice',
		selected_output_device_id: null,
		selected_master_output_device_id: SPEAKERS,
		outputs: SILVER_JACK,
		routes: { master: { state: 'selected', selected: true }, cue: { state: 'default', selected: false } }
	};
	const fault = collision.mainOutputFault(state, HP);
	assert.deepEqual(fault, { kind: 'cannot_sound', deviceId: SPEAKERS, label: 'MacBook Pro Speakers', fixDeviceId: HP });
	assert.match(collision.mainOutputFaultText(fault), /headphone jack has muted/);
	// Control: MAIN on the jack itself raises nothing.
	assert.equal(collision.mainOutputFault({ ...state, selected_master_output_device_id: HP }, HP), null);
});

test('refresh, cue select and the shell release all route through the split and the re-plan', () => {
	const source = readFileSync(HEADPHONES, 'utf8');
	const refresh = source.slice(source.indexOf('export async function refreshHeadphoneOutputs('));
	assert.ok(refresh.indexOf('_enterSameDeviceSplit(') > 0, 'refresh applies a same-device split');
	assert.ok(refresh.indexOf('_exitSameDeviceSplitIfItsOutputVanished()') > 0, 'refresh ends a split whose output vanished');
	const select = source.slice(source.indexOf('export async function selectHeadphoneOutput('));
	assert.ok(
		select.indexOf('_enterSameDeviceSplit(') < select.indexOf("output_mode = 'two_outputs'"),
		'a cue on the room output splits before two_outputs is forced'
	);
	const released = source.slice(source.indexOf("case 'master_pin_released'"));
	assert.ok(released.indexOf('selected_master_output_device_id = null') > 0, 'the released pin is cleared');
	assert.ok(released.indexOf('_onHeadphoneDeviceChange()') > 0, 'and the page re-plans');
	const split = source.slice(source.indexOf('async function _enterSameDeviceSplit('));
	assert.ok(
		split.indexOf('silenceVanishedCueOutput(') < split.indexOf('_applyMasterSink('),
		'the separate cue sink closes before MAIN is pinned to the shared output'
	);
});

test('the split shows a visible line in the TopBar', () => {
	assert.equal(
		collision.sameDeviceSplitText('External Headphones'),
		'Split cue: master L / cue R (same device: External Headphones)'
	);
	assert.match(readFileSync(TOPBAR, 'utf8'), /<SameDeviceSplitBadge \/>/);
	assert.match(readFileSync(BADGE, 'utf8'), /split_reason !== 'same_device'/);
});
