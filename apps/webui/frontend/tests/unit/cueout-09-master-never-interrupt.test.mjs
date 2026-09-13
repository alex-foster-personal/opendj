// requirement: CUEOUT-09
// [if] MASTER is pinned to speakers and the CUE headphones unplug [then] applyMaster is false and the room stays two_outputs master-only
// [if] MASTER is pinned to speakers and CUE headphones lose battery [then] applyMaster is false
// [if] those headphones power back on and the remembered cue id is listed [then] restoreCue is true and applyMaster is still false
// [if] the OS default becomes the headphones while MASTER is already pinned to speakers [then] applyMaster is false and master id stays speakers
// [if] the operator clicked MAIN and the old cue device later reappears [then] restoreCue is false
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));

let headphones;

before(async () => {
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
});

const WIRED = [
	{ id: 'lg', label: 'LG ULTRAWIDE' },
	{ id: 'hp', label: 'External Headphones' },
	{ id: 'speakers', label: 'MacBook Pro Speakers' }
];

const SPEAKERS_ONLY = [
	{ id: 'lg', label: 'LG ULTRAWIDE' },
	{ id: 'speakers', label: 'MacBook Pro Speakers' }
];

const WITH_BT = [
	...WIRED,
	{ id: 'bt', label: 'WH-1000XM5' }
];

test('cue unplug does not re-set a still-present MASTER and keeps the room master-only', () => {
	const plan = headphones.pinnedSinkReapplyPlan({
		previousMasterId: 'speakers',
		nextMasterId: 'speakers',
		previousCueId: 'hp',
		nextCueId: null,
		rememberedCueId: 'hp',
		cueClearedByOperator: false,
		outputs: SPEAKERS_ONLY
	});
	assert.deepEqual(plan, {
		applyMaster: false,
		applyCue: false,
		clearCue: true,
		restoreCue: false,
		keepTwoOutputs: true
	});
	assert.deepEqual(headphones.practiceMainGains('two_outputs', null, 0), {
		cue: 0,
		master: 1
	});
});

test('cue battery-die is the same vanish as unplug: MASTER is not re-set', () => {
	const plan = headphones.pinnedSinkReapplyPlan({
		previousMasterId: 'speakers',
		nextMasterId: 'speakers',
		previousCueId: 'bt',
		nextCueId: null,
		rememberedCueId: 'bt',
		cueClearedByOperator: false,
		outputs: SPEAKERS_ONLY
	});
	assert.equal(plan.applyMaster, false);
	assert.equal(plan.clearCue, true);
	assert.equal(plan.restoreCue, false);
	assert.equal(plan.keepTwoOutputs, true);
});

test('cue power-back-on restores CUE and never re-sets MASTER', () => {
	const plan = headphones.pinnedSinkReapplyPlan({
		previousMasterId: 'speakers',
		nextMasterId: 'speakers',
		previousCueId: null,
		nextCueId: null,
		rememberedCueId: 'bt',
		cueClearedByOperator: false,
		outputs: WITH_BT
	});
	assert.deepEqual(plan, {
		applyMaster: false,
		applyCue: true,
		clearCue: false,
		restoreCue: true,
		keepTwoOutputs: true
	});
});

test('OS default becoming headphones does not move a pinned MASTER', () => {
	assert.deepEqual(
		headphones.dualSinkAssignment({
			outputs: WIRED,
			selectedCueId: 'hp',
			selectedMasterId: 'speakers'
		}),
		{ masterId: 'speakers', cueId: 'hp', autoPinnedMaster: false }
	);
	const plan = headphones.pinnedSinkReapplyPlan({
		previousMasterId: 'speakers',
		nextMasterId: 'speakers',
		previousCueId: 'hp',
		nextCueId: 'hp',
		rememberedCueId: 'hp',
		cueClearedByOperator: false,
		outputs: WIRED
	});
	assert.equal(plan.applyMaster, false);
	assert.equal(plan.applyCue, false);
	assert.equal(plan.clearCue, false);
	assert.equal(plan.restoreCue, false);
});

test('operator MAIN click blocks cue restore when the old device reappears', () => {
	const plan = headphones.pinnedSinkReapplyPlan({
		previousMasterId: 'speakers',
		nextMasterId: 'speakers',
		previousCueId: null,
		nextCueId: null,
		rememberedCueId: 'hp',
		cueClearedByOperator: true,
		outputs: WIRED
	});
	assert.equal(plan.restoreCue, false);
	assert.equal(plan.applyMaster, false);
	assert.equal(plan.keepTwoOutputs, false);
});

test('first auto-pin (master was null) is the only devicechange that may set MASTER', () => {
	const plan = headphones.pinnedSinkReapplyPlan({
		previousMasterId: null,
		nextMasterId: 'speakers',
		previousCueId: null,
		nextCueId: 'hp',
		rememberedCueId: 'hp',
		cueClearedByOperator: false,
		outputs: WIRED
	});
	assert.equal(plan.applyMaster, true);
	assert.equal(plan.applyCue, true);
	assert.equal(plan.restoreCue, false);
});

test('devicechange reapply never calls master setSinkId unless the pinned id changed', () => {
	const source = readFileSync(`${FRONTEND}/src/lib/player/headphones.ts`, 'utf8');
	assert.match(source, /pinnedSinkReapplyPlan/);
	assert.match(source, /if \(plan\.applyMaster\)/);
	assert.match(source, /if \(plan\.keepTwoOutputs\)/);
	assert.match(source, /_rememberedCueId/);
	assert.match(source, /_cueClearedByOperator/);
	assert.doesNotMatch(
		source,
		/reconciled\.selected_output_device_id === null && mixerState\.headphones\.output_mode === 'two_outputs'/
	);
	assert.match(source, /MAIN speaker line cannot be interrupted/);
});
