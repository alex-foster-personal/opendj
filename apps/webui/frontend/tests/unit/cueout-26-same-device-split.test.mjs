// requirement: CUEOUT-26 (amended by AUDFID-01: one shared output refuses the CUE, never an automatic split)
// [if] MAIN and CUE are one physical output (the same id, or the MacBook speakers and the occupied headphone jack) [then] dualSinkAssignment refuses the CUE with the explicit split cue fix named, [else stop]
// [if] MAIN and CUE are genuinely different devices [then] two-output routing is kept with no split, [else stop]
// [if] the shell says an output is muted by the jack [then] it is never auto-picked and an explicit MAIN pick of it is refused, [else stop]
// [if] the shell releases a MAIN pin [then] the page clears the pin, says why, and re-plans, [else stop]
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

/** AUDFID-01: one physical output for MAIN and CUE refuses the CUE. */
const refusedOnHp = (masterId) => ({
	masterId,
	cueId: null,
	boothId: null,
	autoPinnedMaster: false,
	cueRefusal: headphones.sameDeviceCueRefusalText('External Headphones')
});

test('if the built-in pair with the jack occupied still auto-splits then the master can go mono, left only (AUDFID-01)', () => {
	// The build-10 sequence: cue picked on the jack while the jack was the default.
	assert.deepEqual(
		headphones.dualSinkAssignment({ outputs: SILVER_JACK, selectedCueId: HP, selectedMasterId: null, currentRoomId: HP }),
		refusedOnHp(null)
	);
	// The CUEOUT-25 repair had pinned MAIN on the muted speakers: still one output.
	assert.deepEqual(
		headphones.dualSinkAssignment({ outputs: SILVER_JACK, selectedCueId: HP, selectedMasterId: SPEAKERS, currentRoomId: HP }),
		refusedOnHp(null)
	);
	// The end state: the muted speakers are the macOS default.
	assert.deepEqual(
		headphones.dualSinkAssignment({ outputs: SILVER_JACK, selectedCueId: HP, selectedMasterId: null, currentRoomId: SPEAKERS }),
		refusedOnHp(null)
	);
	// An identical id is one output too: MAIN keeps it, the CUE is refused.
	assert.deepEqual(
		headphones.dualSinkAssignment({ outputs: SILVER_JACK, selectedCueId: HP, selectedMasterId: HP, currentRoomId: HP }),
		refusedOnHp(HP)
	);
});

test('control: genuinely different devices keep two outputs, and MAIN is never auto-pinned in the Mac app', () => {
	// An explicit MAIN on the display is the operator's choice: kept.
	assert.deepEqual(
		headphones.dualSinkAssignment({ outputs: SILVER_JACK, selectedCueId: HP, selectedMasterId: LG, currentRoomId: HP }),
		{ masterId: LG, cueId: HP, boothId: null, autoPinnedMaster: false }
	);
	// A USB interface present does not move the room to it: the room is the jack,
	// so a CUE on the jack is refused rather than MAIN being moved (no hidden default).
	const withUsb = [
		...SILVER_JACK,
		...sink.nativeOutputsAsHeadphoneOutputs([
			{ uid: 'scarlett', name: 'Scarlett 2i2', channels: 2, transport: 'usb', is_default: false, muted_by_jack: false, physical_uid: 'scarlett' }
		])
	];
	assert.deepEqual(
		headphones.dualSinkAssignment({ outputs: withUsb, selectedCueId: HP, selectedMasterId: null, currentRoomId: HP }),
		refusedOnHp(null)
	);
	assert.deepEqual(
		headphones.dualSinkAssignment({ outputs: withUsb, selectedCueId: HP, selectedMasterId: null, currentRoomId: 'native:scarlett' }),
		{ masterId: null, cueId: HP, boothId: null, autoPinnedMaster: false }
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
