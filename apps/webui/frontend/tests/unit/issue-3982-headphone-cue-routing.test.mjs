// requirement: UX-EXPLAIN-02
// [if] MAIN practice, no cue device, channel CUE on, MIX full cue [then] cue blends to speakers, not silent
// [if] two_outputs with active monitor and MIX full cue [then] cue is on phones only, room stays master-only
// [if] SPLIT with channel CUE on and MIX full cue [then] cue routes to the right leg only
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let headphones;

before(async () => {
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
});

test('pin fe53412e2a76: practice MAIN routes channel CUE into the speaker blend', () => {
	const targets = headphones.headphoneMixTargetGains({
		output_mode: 'practice',
		selected_output_device_id: null,
		mix: 0,
		level: 1,
		active: false
	});
	assert.equal(targets.monitorLive, false);
	assert.equal(targets.monitorCueMix, 0);
	assert.equal(targets.practiceCueMix, 1);
	assert.equal(targets.practiceMasterMix, 1);
	assert.equal(targets.splitRightCue, 0);
});

test('pin fe53412e2a76: two_outputs sends cue to the monitor path, not the room', () => {
	const targets = headphones.headphoneMixTargetGains({
		output_mode: 'two_outputs',
		selected_output_device_id: 'usb-hp',
		mix: 0,
		level: 1,
		active: true
	});
	assert.equal(targets.monitorLive, true);
	assert.equal(targets.monitorCueMix, 1);
	assert.equal(targets.monitorMasterMix, 0);
	assert.equal(targets.practiceCueMix, 0);
	assert.equal(targets.practiceMasterMix, 1);
});

test('pin fe53412e2a76: inactive two_outputs monitor is silent until HEADPHONE CUE is live', () => {
	const targets = headphones.headphoneMixTargetGains({
		output_mode: 'two_outputs',
		selected_output_device_id: 'usb-hp',
		mix: 0,
		level: 1,
		active: false
	});
	assert.equal(targets.monitorLive, false);
	assert.equal(targets.monitorCueMix, 0);
	assert.equal(targets.practiceCueMix, 0);
});

test('pin fe53412e2a76: split cable puts cue on the right leg with GAIN scaling', () => {
	const targets = headphones.headphoneMixTargetGains({
		output_mode: 'split_cable',
		selected_output_device_id: null,
		mix: 0,
		level: 0.75,
		active: false
	});
	assert.equal(targets.splitLeft, 1);
	assert.equal(targets.splitRightCue, 0.75);
	assert.equal(targets.splitRightMaster, 0);
	assert.equal(targets.practiceCueMix, 0);
});

test('channel CUE still reaches cueSum when the graph is built', async () => {
	const [graphSrc, engineSrc] = await Promise.all([
		readFile('src/lib/rb/deck-channel-graph.ts', 'utf8'),
		readFile('src/lib/rb/audio-engine.svelte.ts', 'utf8')
	]);
	assert.match(graphSrc, /cue\.connect\(deps\.cueSum\)/);
	assert.match(engineSrc, /setChannelCue\(deck: DeckId, enabled: boolean\)/);
	assert.match(engineSrc, /_setParam\(nodes\.cue\.gain, enabled \? 1 : 0\)/);
	assert.match(engineSrc, /cueSum: headphones\.cueSum/);
});

test('leaving two_outputs for practice clears the cue device selection', async () => {
	const hpSrc = await readFile('src/lib/player/headphones.ts', 'utf8');
	assert.match(hpSrc, /if \(mode !== 'two_outputs'\) \{\s*_clearHeadphoneSelection\(\);/);
});
