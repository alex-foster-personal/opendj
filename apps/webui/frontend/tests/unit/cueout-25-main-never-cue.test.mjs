// requirement: CUEOUT-25
// [if] a saved MAIN equals the HEADPHONE CUE device [then] dualSinkAssignment runs split cue on that device (CUEOUT-26 superseded the CUEOUT-25 re-pick), [else stop]
// [if] MAIN and CUE are distinct present devices [then] the saved MAIN is kept, no banner fault and no repair are raised, [else stop]
// [if] selectMasterOutput is asked for the CUE device in two_outputs [then] it enters split cue on that device instead of a silent room (CUEOUT-26), [else stop]
// [if] MAIN equals CUE in two_outputs [then] mainOutputFault names that device and offers a room fix, [else stop]
// [if] a refresh repairs a conflicting saved MAIN [then] the repaired MAIN is persisted after it is pinned, [else stop]
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));
const HEADPHONES = `${FRONTEND}/src/lib/player/headphones.ts`;
const TOPBAR = `${FRONTEND}/src/lib/components/rb/TopBar.svelte`;
const BANNER = `${FRONTEND}/src/lib/components/rb/MainOutputFaultBanner.svelte`;

let headphones;
let collision;

before(async () => {
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	collision = await loadTypeScriptModule('src/lib/player/main-cue-collision.ts');
});

const SILVER = [
	{ id: 'native:BuiltInHeadphoneOutputDevice', label: 'External Headphones' },
	{ id: 'native:BuiltInSpeakerDevice', label: 'MacBook Pro Speakers' },
	{ id: 'native:lg', label: 'LG ULTRAWIDE' }
];
const HP = 'native:BuiltInHeadphoneOutputDevice';
const SPEAKERS = 'native:BuiltInSpeakerDevice';

function headphoneState(overrides) {
	return {
		output_mode: 'two_outputs',
		selected_output_device_id: HP,
		selected_master_output_device_id: SPEAKERS,
		outputs: SILVER,
		routes: { master: { state: 'selected', selected: true }, cue: { state: 'selected', selected: true } },
		...overrides
	};
}

test('a saved MAIN equal to the CUE device runs split cue on it (CUEOUT-26), never a silent room', () => {
	const split = { masterId: HP, cueId: HP, autoPinnedMaster: false, splitSameDevice: true };
	assert.deepEqual(headphones.dualSinkAssignment({ outputs: SILVER, selectedCueId: HP, selectedMasterId: HP }), split);
	assert.deepEqual(
		headphones.dualSinkAssignment({ outputs: SILVER, selectedCueId: HP, selectedMasterId: HP, currentRoomId: 'native:lg' }),
		split,
		'an explicit MAIN on the cue device is the operator saying one output'
	);
	// Control: an UNPINNED MAIN with the room elsewhere still gets the room.
	assert.deepEqual(
		headphones.dualSinkAssignment({ outputs: SILVER, selectedCueId: HP, selectedMasterId: null, currentRoomId: 'native:lg' }),
		{ masterId: 'native:lg', cueId: HP, autoPinnedMaster: true }
	);
});

test('control: a distinct saved MAIN is kept and not re-pinned', () => {
	assert.deepEqual(
		headphones.dualSinkAssignment({ outputs: SILVER, selectedCueId: HP, selectedMasterId: 'native:lg' }),
		{ masterId: 'native:lg', cueId: HP, autoPinnedMaster: false }
	);
});

test('selectMasterOutput turns MAIN-on-the-CUE-device into split cue before its try, so the live MAIN route is never failed', () => {
	const source = readFileSync(HEADPHONES, 'utf8');
	const body = source.slice(source.indexOf('export async function selectMasterOutput('));
	const split = body.indexOf('_enterSameDeviceSplit(');
	const sameCheck = body.indexOf('sameOutputDevice(mixerState.headphones.outputs, deviceId, cueId)');
	const tryAt = body.indexOf('try {');
	assert.ok(sameCheck > 0 && split > sameCheck, 'selectMasterOutput must split when MAIN is the CUE output');
	assert.ok(split < tryAt, 'the split must run before the try that marks routes.master failed');
	assert.ok(!source.includes('assertMainIsNotCue('), 'the CUEOUT-25 refusal is superseded, not kept beside the split');
});

test('MAIN equal to CUE raises the banner fault with a room fix', () => {
	const state = headphoneState({ selected_master_output_device_id: HP });
	const fix = headphones.preferredMasterOutputDeviceId(state.outputs, state.selected_output_device_id);
	const fault = collision.mainOutputFault(state, fix);
	assert.deepEqual(fault, { kind: 'same_as_cue', deviceId: HP, label: 'External Headphones', fixDeviceId: SPEAKERS });
	assert.equal(
		collision.mainOutputFaultText(fault),
		'Main output is going to External Headphones (same as headphones). The room gets nothing.'
	);
});

test('a failed MAIN route raises the lost fault', () => {
	const state = headphoneState({ routes: { master: { state: 'failed', selected: true }, cue: { state: 'selected', selected: true } } });
	assert.deepEqual(collision.mainOutputFault(state, SPEAKERS), {
		kind: 'lost',
		label: 'MacBook Pro Speakers',
		fixDeviceId: SPEAKERS
	});
});

test('control: distinct devices, and split_cable on one device, raise no fault', () => {
	assert.equal(collision.mainOutputFault(headphoneState({}), SPEAKERS), null);
	assert.equal(
		collision.mainOutputFault(headphoneState({ output_mode: 'split_cable', selected_master_output_device_id: HP }), SPEAKERS),
		null
	);
});

test('masterRepairedFromCue fires only when a refresh moved MAIN off the CUE device', () => {
	assert.deepEqual(
		collision.masterRepairedFromCue({ previousMasterId: HP, cueId: HP, nextMasterId: SPEAKERS, autoPinnedMaster: true }),
		{ from: HP, to: SPEAKERS }
	);
	// Controls: a first auto-pin from nothing, and a kept distinct MAIN, are not repairs.
	assert.equal(
		collision.masterRepairedFromCue({ previousMasterId: null, cueId: HP, nextMasterId: SPEAKERS, autoPinnedMaster: true }),
		null
	);
	assert.equal(
		collision.masterRepairedFromCue({ previousMasterId: SPEAKERS, cueId: HP, nextMasterId: SPEAKERS, autoPinnedMaster: false }),
		null
	);
	// A vanished non-cue MAIN that gets auto-pinned is a CUEOUT-09 fallback, not a repair from the cue.
	assert.equal(
		collision.masterRepairedFromCue({ previousMasterId: 'native:gone', cueId: HP, nextMasterId: SPEAKERS, autoPinnedMaster: true }),
		null
	);
});

test('refresh persists the repaired MAIN only after the sink is re-pinned', () => {
	const source = readFileSync(HEADPHONES, 'utf8');
	const body = source.slice(source.indexOf('export async function refreshHeadphoneOutputs('));
	const reapply = body.indexOf('await _reapplyPinnedSinks(');
	const persist = body.indexOf("_rememberSavedOutput('master', repaired.to)");
	assert.ok(body.includes('masterRepairedFromCue({'), 'refresh must compute the repair');
	assert.ok(persist > reapply && reapply > 0, 'the repaired MAIN must be persisted after the re-pin');
});

test('the TopBar mounts the persistent banner and its Fix calls engine.selectMasterOutput', () => {
	assert.match(readFileSync(TOPBAR, 'utf8'), /<MainOutputFaultBanner \/>/);
	const banner = readFileSync(BANNER, 'utf8');
	assert.match(banner, /engine\.selectMasterOutput\(fault\.fixDeviceId\)/);
	assert.doesNotMatch(banner, /setTimeout/, 'the banner must not auto-dismiss');
});
