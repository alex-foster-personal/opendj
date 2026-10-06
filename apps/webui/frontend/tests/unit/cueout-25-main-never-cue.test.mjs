// requirement: CUEOUT-25
// [if] a saved MAIN equals the HEADPHONE CUE device [then] dualSinkAssignment re-picks a room output and marks it auto-pinned, [else stop]
// [if] MAIN and CUE are distinct present devices [then] the saved MAIN is kept, no banner fault and no repair are raised, [else stop]
// [if] selectMasterOutput is asked for the CUE device in two_outputs [then] it throws before touching the MAIN route, [else stop]
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

test('a saved MAIN equal to the CUE device is re-picked to the room on load', () => {
	assert.deepEqual(
		headphones.dualSinkAssignment({ outputs: SILVER, selectedCueId: HP, selectedMasterId: HP }),
		{ masterId: SPEAKERS, cueId: HP, autoPinnedMaster: true }
	);
	// The Mac app reports where the room already plays; that wins over a label guess unless it is the cue.
	assert.deepEqual(
		headphones.dualSinkAssignment({ outputs: SILVER, selectedCueId: HP, selectedMasterId: HP, currentRoomId: 'native:lg' }),
		{ masterId: 'native:lg', cueId: HP, autoPinnedMaster: true }
	);
	assert.deepEqual(
		headphones.dualSinkAssignment({ outputs: SILVER, selectedCueId: HP, selectedMasterId: HP, currentRoomId: HP }),
		{ masterId: SPEAKERS, cueId: HP, autoPinnedMaster: true }
	);
});

test('control: a distinct saved MAIN is kept and not re-pinned', () => {
	assert.deepEqual(
		headphones.dualSinkAssignment({ outputs: SILVER, selectedCueId: HP, selectedMasterId: 'native:lg' }),
		{ masterId: 'native:lg', cueId: HP, autoPinnedMaster: false }
	);
});

test('selectMasterOutput guard refuses the CUE device in two_outputs only', () => {
	assert.throws(() => collision.assertMainIsNotCue(HP, HP, 'two_outputs'), /MAIN output cannot be the HEADPHONE CUE device/);
	// Controls: a distinct device, no cue, and split_cable (one device by design) all pass.
	assert.doesNotThrow(() => collision.assertMainIsNotCue(SPEAKERS, HP, 'two_outputs'));
	assert.doesNotThrow(() => collision.assertMainIsNotCue(HP, null, 'two_outputs'));
	assert.doesNotThrow(() => collision.assertMainIsNotCue(HP, HP, 'split_cable'));
});

test('selectMasterOutput runs the guard before its try, so a refusal never fails the live MAIN route', () => {
	const source = readFileSync(HEADPHONES, 'utf8');
	const body = source.slice(source.indexOf('export async function selectMasterOutput('));
	const guard = body.indexOf('assertMainIsNotCue(');
	const tryAt = body.indexOf('try {');
	assert.ok(guard > 0, 'selectMasterOutput must call assertMainIsNotCue');
	assert.ok(guard < tryAt, 'the guard must run before the try that marks routes.master failed');
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
