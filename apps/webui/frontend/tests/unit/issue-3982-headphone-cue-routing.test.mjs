// requirement: UX-EXPLAIN-02
// [if] MAIN practice, no cue device, channel CUE on, MIX full cue [then] cue blends to speakers, not silent
// [if] two_outputs with active monitor and MIX full cue [then] cue is on phones only, room stays master-only
// [if] SPLIT with channel CUE on and MIX full cue [then] cue routes to the right leg only
// [if] a multichannel interface carries the cue while the mode is not two_outputs [then] the monitor stays live
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
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
		active: false,
		multichannel_monitor_active: false
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
		active: true,
		multichannel_monitor_active: false
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
		active: false,
		multichannel_monitor_active: false
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
		active: false,
		multichannel_monitor_active: false
	});
	assert.equal(targets.splitLeft, 1);
	assert.equal(targets.splitRightCue, 0.75);
	assert.equal(targets.splitRightMaster, 0);
	assert.equal(targets.practiceCueMix, 0);
});

test('a multichannel interface keeps the cue monitor live outside two_outputs', () => {
	const targets = headphones.headphoneMixTargetGains({
		output_mode: 'practice',
		selected_output_device_id: null,
		mix: 0,
		level: 0.5,
		active: false,
		multichannel_monitor_active: true
	});
	assert.equal(targets.monitorLive, true);
	assert.equal(targets.monitorCueMix, 1);
	assert.equal(targets.monitorLevel, 0.5);
});

test('practice MAIN keeps speaker cue blend when a stale cue device id remains in state', () => {
	const targets = headphones.headphoneMixTargetGains({
		output_mode: 'practice',
		selected_output_device_id: 'usb-hp',
		mix: 0,
		level: 1,
		active: false,
		multichannel_monitor_active: false
	});
	assert.equal(targets.practiceCueMix, 1);
	assert.equal(targets.practiceMasterMix, 1);
	assert.equal(targets.monitorCueMix, 0);
});

test('buildDeckChannelGraph seeds per-deck cue gain from cue_enabled', async () => {
	const graph = await loadTypeScriptModule('src/lib/rb/deck-channel-graph.ts');
	const param = () => ({ value: 0 });
	const ctx = {
		currentTime: 0,
		sampleRate: 48000,
		createAnalyser() {
			return {
				connect() {},
				disconnect() {},
				fftSize: 2048,
				minDecibels: -120,
				maxDecibels: 0,
				smoothingTimeConstant: 0
			};
		},
		createGain() {
			const gain = param();
			return { gain, connect() {}, disconnect() {} };
		},
		createBiquadFilter() {
			return {
				type: 'lowshelf',
				frequency: param(),
				Q: param(),
				gain: param(),
				connect() {},
				disconnect() {}
			};
		},
		createChannelSplitter() {
			return { connect() {}, disconnect() {} };
		}
	};
	const cueSum = ctx.createGain();
	const mixerState = {
		crossfader: 0.5,
		channels: {
			1: {
				trim: 1,
				eq_low: 0.5,
				eq_mid: 0.5,
				eq_high: 0.5,
				filter: 0.5,
				fader: 1,
				assign: 'THRU',
				cue_enabled: true
			},
			2: { trim: 1, eq_low: 0.5, eq_mid: 0.5, eq_high: 0.5, filter: 0.5, fader: 1, assign: 'THRU', cue_enabled: false },
			3: { trim: 1, eq_low: 0.5, eq_mid: 0.5, eq_high: 0.5, filter: 0.5, fader: 1, assign: 'THRU', cue_enabled: false },
			4: { trim: 1, eq_low: 0.5, eq_mid: 0.5, eq_high: 0.5, filter: 0.5, fader: 1, assign: 'THRU', cue_enabled: false }
		}
	};
	let deck1Cue = null;
	graph.buildDeckChannelGraph({
		ctx,
		mixerState,
		masterGain: ctx.createGain(),
		externalMerger: null,
		routing: null,
		cueSum,
		xfGainFor: () => 1,
		onDeck: (deck, nodes) => {
			if (deck === 1) deck1Cue = nodes.cue;
		}
	});
	assert.equal(deck1Cue.gain.value, 1);
});

test('leaving two_outputs for practice clears the cue device selection', () => {
	const hpSrc = readFileSync('src/lib/player/headphones.ts', 'utf8');
	assert.match(hpSrc, /if \(mode !== 'two_outputs'\) \{\s*_clearHeadphoneSelection\(\);/);
});
