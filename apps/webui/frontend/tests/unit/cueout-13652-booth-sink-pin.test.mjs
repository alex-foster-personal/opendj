// requirement: CUEOUT-13652
// [if] booth B and master A are pinned [then] dualSinkAssignment keeps both ids
// [if] booth level is 0 [then] effectiveBoothGain is 0 and master is unchanged
// [if] CUE or BOOTH vanishes [then] pinnedSinkReapplyPlan does not re-pin MASTER
// [if] booth mode is off or follows_master [then] effectiveBoothGain honors signal_mode
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let headphones;
let boothOutput;
let state;

const OUTPUTS = [
	{ id: 'master-a', label: 'Room' },
	{ id: 'cue-hp', label: 'Headphones' },
	{ id: 'booth-b', label: 'Booth' }
];

before(async () => {
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	boothOutput = await loadTypeScriptModule('src/lib/player/booth-output.ts');
	state = await loadTypeScriptModule('src/lib/player/state.svelte.ts');
});

test('booth B and master A pin without moving each other', () => {
	assert.deepEqual(
		headphones.dualSinkAssignment({
			outputs: OUTPUTS,
			selectedMasterId: 'master-a',
			selectedCueId: 'cue-hp',
			selectedBoothId: 'booth-b'
		}),
		{ masterId: 'master-a', cueId: 'cue-hp', boothId: 'booth-b', autoPinnedMaster: false }
	);
});

test('booth level 0 silences booth gain only', () => {
	const masterBefore = state.mixerState.master;
	const cueBefore = state.mixerState.headphones.level;
	state.mixerState.booth.signal_mode = 'independent';
	state.mixerState.booth.level = 0.5;
	headphones.setBoothLevel(0);
	assert.equal(boothOutput.effectiveBoothGain(), 0);
	assert.equal(state.mixerState.master, masterBefore);
	assert.equal(state.mixerState.headphones.level, cueBefore);
});

test('vanishing CUE or BOOTH never sets applyMaster', () => {
	const cueVanish = headphones.pinnedSinkReapplyPlan({
		previousMasterId: 'master-a',
		nextMasterId: 'master-a',
		previousCueId: 'cue-hp',
		nextCueId: null,
		rememberedCueId: 'cue-hp',
		cueClearedByOperator: false,
		previousBoothId: 'booth-b',
		nextBoothId: 'booth-b',
		rememberedBoothId: 'booth-b',
		boothClearedByOperator: false,
		outputs: OUTPUTS
	});
	assert.equal(cueVanish.applyMaster, false);
	assert.equal(cueVanish.clearCue, true);

	const boothVanish = headphones.pinnedSinkReapplyPlan({
		previousMasterId: 'master-a',
		nextMasterId: 'master-a',
		previousCueId: 'cue-hp',
		nextCueId: 'cue-hp',
		rememberedCueId: 'cue-hp',
		cueClearedByOperator: false,
		previousBoothId: 'booth-b',
		nextBoothId: null,
		rememberedBoothId: 'booth-b',
		boothClearedByOperator: false,
		outputs: OUTPUTS
	});
	assert.equal(boothVanish.applyMaster, false);
	assert.equal(boothVanish.clearBooth, true);
});

test('booth signal_mode off and follows_master drive effective gain', () => {
	state.mixerState.booth.level = 0.8;
	state.mixerState.master = 0.5;
	state.mixerState.booth.signal_mode = 'off';
	assert.equal(boothOutput.effectiveBoothGain(), 0);

	headphones.setBoothSignalMode('follows_master');
	assert.equal(boothOutput.effectiveBoothGain(), 0.4);

	headphones.setBoothSignalMode('independent');
	assert.equal(boothOutput.effectiveBoothGain(), 0.8);
});
