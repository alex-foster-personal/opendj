// requirement: CUEOUT-13
// [if] a third output is picked as BOOTH [then] it receives MASTER, never the cue bus
// [if] BOOTH is unset [then] only the compact Add booth control occupies the I/O menu
// [if] BOOTH vanishes while pinned [then] MASTER is not re-pinned and BOOTH alone is cleared
// [if] BOOTH GAIN changes [then] MASTER and HEADPHONE CUE levels are unchanged
// [if] BOOTH is selected first [then] no cue calibration chirp is started
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));
const HEADPHONES = `${FRONTEND}/src/lib/player/headphones.ts`;

let headphones;
let state;

before(async () => {
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	state = await loadTypeScriptModule('src/lib/player/state.svelte.ts');
});

const OUTPUTS = [
	{ id: 'speakers', label: 'Room speakers' },
	{ id: 'phones', label: 'Headphones' },
	{ id: 'booth-usb', label: 'Booth monitors' }
];

test('a third distinct sink is assigned to BOOTH without moving MAIN or CUE', () => {
	assert.deepEqual(
		headphones.dualSinkAssignment({
			outputs: OUTPUTS,
			selectedMasterId: 'speakers',
			selectedCueId: 'phones',
			selectedBoothId: 'booth-usb'
		}),
		{ masterId: 'speakers', cueId: 'phones', boothId: 'booth-usb', autoPinnedMaster: false }
	);
});

test('BOOTH refuses a physical MAIN or HEADPHONE CUE collision', () => {
	const onMain = headphones.dualSinkAssignment({
		outputs: OUTPUTS,
		selectedMasterId: 'speakers',
		selectedCueId: 'phones',
		selectedBoothId: 'speakers'
	});
	assert.equal(onMain.boothId, null);
	assert.match(onMain.boothRefusal, /BOOTH and MAIN are the same output/);
	const onCue = headphones.dualSinkAssignment({
		outputs: OUTPUTS,
		selectedMasterId: 'speakers',
		selectedCueId: 'phones',
		selectedBoothId: 'phones'
	});
	assert.equal(onCue.boothId, null);
	assert.match(onCue.boothRefusal, /BOOTH and HEADPHONE CUE are the same output/);
});

test('BOOTH unplug and power-back-on never re-pin MASTER', () => {
	const unplug = headphones.pinnedSinkReapplyPlan({
		previousMasterId: 'speakers',
		nextMasterId: 'speakers',
		previousCueId: 'phones',
		nextCueId: 'phones',
		rememberedCueId: 'phones',
		cueClearedByOperator: false,
		previousBoothId: 'booth-usb',
		nextBoothId: null,
		rememberedBoothId: 'booth-usb',
		boothClearedByOperator: false,
		outputs: OUTPUTS.slice(0, 2)
	});
	assert.equal(unplug.applyMaster, false);
	assert.equal(unplug.clearBooth, true);
	assert.equal(unplug.applyCue, false);

	const restored = headphones.pinnedSinkReapplyPlan({
		previousMasterId: 'speakers',
		nextMasterId: 'speakers',
		previousCueId: 'phones',
		nextCueId: 'phones',
		rememberedCueId: 'phones',
		cueClearedByOperator: false,
		previousBoothId: null,
		nextBoothId: null,
		rememberedBoothId: 'booth-usb',
		boothClearedByOperator: false,
		outputs: OUTPUTS
	});
	assert.equal(restored.applyMaster, false);
	assert.equal(restored.restoreBooth, true);
});

test('BOOTH GAIN changes only booth state', () => {
	const hp = state.mixerState.headphones;
	const cueLevel = hp.level;
	const masterLevel = state.mixerState.master;
	headphones.setBoothLevel(0);
	assert.equal(state.mixerState.booth.level, 0);
	assert.equal(hp.level, cueLevel);
	assert.equal(state.mixerState.master, masterLevel);
});

test('BOOTH graph taps the room path and first select contains no calibration chirp', () => {
	const source = readFileSync(HEADPHONES, 'utf8');
	assert.match(source, /masterGain\.connect\(boothMute\)/);
	assert.match(source, /boothMute\.connect\(boothDelay\)/);
	assert.match(source, /boothDelay\.connect\(boothGain\)/);
	assert.match(source, /boothDelay\.delayTime\.setValueAtTime/);
	assert.match(source, /attachExtraMasterMuteNode\(boothMute\)/);
	assert.match(source, /attachExtraMasterMuteNode\(null\)/);
	assert.doesNotMatch(source, /cueSum\.connect\(boothGain\)|bridgeInput\.connect\(boothGain\)/);
	assert.doesNotMatch(source, /_masterDelayNode\.connect\(boothGain\)/);
	assert.doesNotMatch(source, /practiceCueMix\.connect\(booth/);
	assert.doesNotMatch(source, /splitMerger\.connect\(booth/);
	const select = source.slice(
		source.indexOf('export async function selectBoothOutput'),
		source.indexOf('export async function selectHeadphoneOutput')
	);
	assert.ok(select.length > 0);
	assert.doesNotMatch(select, /startCueAlignment|calibrat|chirp/i);
});
