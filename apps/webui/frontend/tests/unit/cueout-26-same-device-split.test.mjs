// requirement: CUEOUT-26
// [if] MAIN and CUE are one physical output (the same id, or the MacBook speakers and the occupied headphone jack) [then] dualSinkAssignment runs split cue on that output, [else stop]
// [if] MAIN and CUE are genuinely different devices [then] two-output routing is kept with no split, [else stop]
// [if] the shell says an output is muted by the jack [then] it is never auto-picked and an explicit MAIN pick of it is refused, [else stop]
// [if] a refresh's split fails after a newer MAIN or CUE pick retired it [then] the failure is logged, never published over the newer pick, [else stop]
// [if] the shell releases a MAIN pin [then] the page clears the pin, says why, and re-plans, [else stop]
// [if] a refresh or CUE pick enters the split and a MAIN pick lands during its sink move [then] the MAIN pick wins and the split applies and saves nothing, [else stop]
// [if] closing the old separate CUE output fails after a split committed [then] the split stands, the pick does not fail, and the failure is shown while the split is newest, [else stop]
// [if] split cue runs because of one shared output [then] the TopBar shows a badge whose tooltip says "Split cue: master L / cue R (same device ...)" (CUEOUT-28 made the visible text compact), [else stop]
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));
const HEADPHONES = `${FRONTEND}/src/lib/player/headphones.ts`;
const TOPBAR = `${FRONTEND}/src/lib/components/rb/TopBar.svelte`;
const BADGE = `${FRONTEND}/src/lib/components/rb/SameDeviceSplitBadge.svelte`;
const BANNER = `${FRONTEND}/src/lib/components/rb/MainOutputFaultBanner.svelte`;

let headphones;
let collision;
let sink;

let SILVER_JACK;

before(async () => {
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	collision = await loadTypeScriptModule('src/lib/player/main-cue-collision.ts');
	sink = await loadTypeScriptModule('src/lib/player/cue-native-sink.ts');
	SILVER_JACK = silverJack();
});

const HP = 'native:BuiltInHeadphoneOutputDevice';
const SPEAKERS = 'native:BuiltInSpeakerDevice';
const LG = 'native:lg';

/** silver, Tue 6 Oct 2026 (build 10), as the Mac shell lists it: wired
 * headphones in the MacBook's own jack mute the speakers. */
const silverJack = () => sink.nativeOutputsAsHeadphoneOutputs([
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
	// The mute alone excludes the speakers, not only their shared jack: with no
	// cue, or a cue on a different device, the muted speakers are still no room.
	assert.equal(headphones.preferredMasterOutputDeviceId(SILVER_JACK, null), null);
	const usbPhones = [
		...SILVER_JACK,
		...sink.nativeOutputsAsHeadphoneOutputs([
			{ uid: 'usb-phones', name: 'USB Headphones', channels: 2, transport: 'usb', is_default: false, muted_by_jack: false, physical_uid: 'usb-phones' }
		])
	];
	assert.notEqual(headphones.preferredMasterOutputDeviceId(usbPhones, 'native:usb-phones'), SPEAKERS);
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

test('every same-device split takes a MAIN serial, so a MAIN pick during its move wins', () => {
	const source = readFileSync(HEADPHONES, 'utf8');
	const helper = source.indexOf('async function _enterSameDeviceSplit(');
	assert.ok(helper > 0);
	// The guard is required, not defaulted, so a caller cannot opt out.
	assert.match(
		source.slice(helper, source.indexOf('): Promise<boolean>', helper) + 1),
		/isCurrent: \(\) => boolean(,\s*cueClaim: \{ serial: number \} \| null = null)?\s*\)$/
	);
	const calls = [...source.matchAll(/await _enterSameDeviceSplit\(([\s\S]*?)\);/g)];
	assert.equal(calls.length, 3, 'refresh, CUE select and MAIN select each enter the split');
	for (const call of calls) {
		assert.match(call[1], /splitSerial === _masterSelectSerial/, `unguarded split call: ${call[1].trim()}`);
		const before = source.slice(Math.max(0, call.index - 400), call.index);
		assert.match(before, /const splitSerial = \+\+_masterSelectSerial;/, 'the split must start a new MAIN serial');
	}
	// Control: the CUE select path stops before saving when its split was superseded.
	const select = source.slice(source.indexOf('export async function selectHeadphoneOutput('));
	const guard = select.search(/if \(!applied( \|\| cueSuperseded\(\))?\) return;/);
	const save = select.indexOf("_rememberSavedOutput('cue', deviceId)");
	assert.ok(guard >= 0, 'the CUE select split has its superseded guard');
	assert.ok(save >= 0, 'the CUE select split saves its CUE');
	assert.ok(guard < save, 'the guard runs before the save');
});

test('the split shows a visible line in the TopBar', () => {
	assert.equal(
		collision.sameDeviceSplitText('External Headphones'),
		'Split cue: master L / cue R (same device: External Headphones)'
	);
	// The badge rides in MainOutputFaultBanner, which the TopBar mounts.
	assert.match(readFileSync(TOPBAR, 'utf8'), /<MainOutputFaultBanner \/>/);
	assert.match(readFileSync(BANNER, 'utf8'), /<SameDeviceSplitBadge \/>/);
	assert.match(readFileSync(BADGE, 'utf8'), /split_reason !== 'same_device'/);
});

test('a split whose old CUE output fails to close keeps the split and says the output may still sound', async () => {
	const suspendRefused = { cueContext: { state: 'running', suspend: () => Promise.reject(new Error('suspend refused')) } };
	const closeRefused = { cueContext: null, nativeCue: { close: () => Promise.reject(new Error('close refused')) } };
	for (const nodes of [suspendRefused, closeRefused]) {
		const warning = await headphones.closeCueOutputForSplit(nodes);
		assert.match(warning ?? '', /old CUE output did not close .*may still sound/);
	}
	// Control: a clean close warns of nothing.
	assert.equal(await headphones.closeCueOutputForSplit({ cueContext: { state: 'running', suspend: async () => {} } }), null);
	// The split routes its teardown through that helper after it commits,
	// shows the warning only while it is the newest pick, and still succeeds.
	const source = readFileSync(HEADPHONES, 'utf8');
	const body = source.slice(source.indexOf('async function _enterSameDeviceSplit('), source.indexOf('export async function closeCueOutputForSplit('));
	assert.ok(body.includes('await closeCueOutputForSplit(cueNodes'));
	assert.ok(!body.includes('await silenceVanishedCueOutput('), 'a raw teardown await would fail a committed split');
	assert.ok(body.indexOf("output_mode = 'split_cable'") < body.indexOf('await closeCueOutputForSplit('));
	assert.ok(body.includes('if (closeFailure !== null && !torndown() && isCurrent()) {'));
	assert.match(body.slice(body.indexOf('await closeCueOutputForSplit(')), /^[^]*?return true;/);
});

test("a refresh's split that a newer pick retired logs its failure instead of publishing it", () => {
	const source = readFileSync(HEADPHONES, 'utf8');
	const refresh = source.slice(source.indexOf('export async function refreshHeadphoneOutputs('));
	const split = refresh.slice(refresh.indexOf('await _enterSameDeviceSplit('));
	const failure = split.slice(split.indexOf('} catch (error) {'));
	const stale = failure.search(/if \(refresh !== _outputRefreshSerial \|\| splitSerial !== _masterSelectSerial \|\| cue\.serial !== _cueSelectSerial\) \{\s*_logSupersededPick\(_detail\(error\)\);\s*return;/);
	const publish = failure.indexOf("_headphoneError('split cue on the shared output failed'");
	assert.ok(stale >= 0, 'the retired-split check is there');
	assert.ok(publish >= 0, 'the current split still reports its failure');
	assert.ok(stale < publish, 'the check runs before the failure is published');
});
