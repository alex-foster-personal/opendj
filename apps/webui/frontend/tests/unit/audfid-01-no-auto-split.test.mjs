// requirement: AUDFID-01 (split cue only by an explicit operator choice)
// [if] a CUE pick lands on MAIN's output [then] it is refused before the try and the mode stays stereo two outputs or practice, never split_cable ⛔️
// [if] a MAIN pick lands on the CUE's output [then] it is refused and MAIN keeps playing ⛔️
// [if] a device change lists MAIN and CUE on one output [then] the CUE leaves the live route with a notice, the mode never becomes split_cable ⛔️
// [if] the Mac app picks a CUE with MAIN unpinned [then] MAIN is not auto-pinned to the macOS default ⛔️
// [if] anything but the operator writes split_cable [then] it throws ⛔️
// [if] output_mode changes [then] the change and its reason are logged ⛔️
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const HEADPHONES = new URL('../../src/lib/player/headphones.ts', import.meta.url);
let hp;
before(async () => {
	hp = await loadTypeScriptModule('tests/unit/fixtures/headphone-picks-entry.ts');
});

const OUTPUTS = [
	{ id: 'native:hyperboom', label: 'HYPERBOOM' },
	{ id: 'native:sony', label: 'af-sony-fun' },
	{ id: 'native:spk', label: 'MacBook Pro Speakers' }
];

test('if a same-device MAIN/CUE pair is assigned a split then the master goes mono, left only', () => {
	const shared = hp.dualSinkAssignment({
		outputs: OUTPUTS,
		selectedCueId: 'native:sony',
		selectedMasterId: 'native:sony',
		currentRoomId: 'native:sony'
	});
	assert.deepEqual(shared, {
		masterId: 'native:sony',
		cueId: null,
		autoPinnedMaster: false,
		cueRefusal: hp.sameDeviceCueRefusalText('af-sony-fun')
	});
});

test('if the Mac app auto-pins MAIN to the macOS default then the room follows whatever grabbed the default', () => {
	// 13:37Z shape: MAIN unpinned, the Bluetooth headphones are the macOS default.
	const onDefault = hp.dualSinkAssignment({
		outputs: OUTPUTS,
		selectedCueId: 'native:sony',
		selectedMasterId: null,
		currentRoomId: 'native:sony'
	});
	assert.equal(onDefault.masterId, null, 'no hidden MAIN pin');
	assert.equal(onDefault.cueId, null, 'the CUE on the room output is refused');
	const elsewhere = hp.dualSinkAssignment({
		outputs: OUTPUTS,
		selectedCueId: 'native:sony',
		selectedMasterId: null,
		currentRoomId: 'native:hyperboom'
	});
	assert.deepEqual(elsewhere, { masterId: null, cueId: 'native:sony', autoPinnedMaster: false });
});

test('if the operator cannot still choose split cue then the explicit route is gone too', () => {
	const state = hp.mixerState.headphones;
	hp.setHeadphoneOutputMode('split_cable');
	assert.equal(state.output_mode, 'split_cable');
	hp.setHeadphoneOutputMode('two_outputs');
	assert.equal(state.output_mode, 'two_outputs');
});

test('if any writer but the operator can set split_cable then an automatic split can come back', () => {
	const source = readFileSync(HEADPHONES, 'utf8');
	const writes = [...source.matchAll(/output_mode = /g)].length;
	assert.equal(writes, 1, 'every output_mode write goes through _setOutputMode');
	assert.match(
		source,
		/if \(mode === 'split_cable' && !_operatorChoseSplit\) \{\n\t\tthrow new Error/
	);
	assert.match(
		source,
		/recordPerfEvent\('output-mode-change', `output_mode \$\{from\} -> \$\{mode\} \(reason \$\{reason\}\)`/
	);
	assert.ok(!source.includes('_enterSameDeviceSplit'), 'no automatic split path is left');
	assert.ok(!source.includes("split_reason = 'same_device'"), 'nothing marks an automatic split');
});

test('if a same-device CUE pick is not refused before its try then a refused pick can roll the mode into split', () => {
	const source = readFileSync(HEADPHONES, 'utf8');
	const select = source.slice(source.indexOf('export async function selectHeadphoneOutput('));
	const refusal = select.indexOf('_assertCueNotOnMainOutput(deviceId);');
	assert.ok(refusal > 0 && refusal < select.indexOf('try {'));
	const main = source.slice(source.indexOf('export async function selectMasterOutput('));
	assert.ok(main.indexOf('throw new Error(refusal);') < main.indexOf('try {'));
});

test('if a device change that puts CUE on MAIN keeps the CUE live or restores it then the room plays the cue mix', () => {
	const source = readFileSync(HEADPHONES, 'utf8');
	const refresh = source.slice(source.indexOf('export async function refreshHeadphoneOutputs('));
	assert.match(
		refresh,
		/if \(assignment\.cueRefusal !== undefined\) \{\n\t\tmixerState\.headphones\.selected_output_device_id = null;/
	);
	assert.match(
		refresh,
		/rememberedCueId: assignment\.cueRefusal === undefined \? _rememberedCueId : null,/
	);
	assert.match(refresh, /access\.notices\.push\(assignment\.cueRefusal\);/);
});
