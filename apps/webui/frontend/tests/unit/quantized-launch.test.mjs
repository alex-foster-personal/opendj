import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const MASTER_BEATS = [
	{ n: 1, bpm: 120, t: 0 },
	{ n: 2, bpm: 120, t: 0.5 },
	{ n: 3, bpm: 120, t: 1.0 },
	{ n: 4, bpm: 120, t: 1.5 },
	{ n: 1, bpm: 120, t: 2.0 },
	{ n: 2, bpm: 120, t: 2.5 }
];

const FOLLOWER_BEATS = [
	{ n: 1, bpm: 120, t: 0.1 },
	{ n: 2, bpm: 120, t: 0.6 },
	{ n: 3, bpm: 120, t: 1.1 },
	{ n: 4, bpm: 120, t: 1.6 },
	{ n: 1, bpm: 120, t: 2.1 }
];

let ql;

before(async () => {
	ql = await loadTypeScriptModule('src/lib/player/transport/quantized-launch.ts');
});

// REQ: LATENCY-02
test('LATENCY-02 planQuantizedLaunch arms on the next shared master beat 1', () => {
	const plan = ql.planQuantizedLaunch({
		nowContextTimeSec: 10,
		processorLeadSec: 0.05,
		masterPlaying: true,
		masterBeats: MASTER_BEATS,
		masterPositionSec: 1.8,
		masterTempoRatio: 1,
		followerPlaying: false,
		followerBeats: FOLLOWER_BEATS,
		followerPositionSec: 0.3
	});
	assert.equal(plan.kind, 'armed');
	assert.equal(plan.followerStartSec, 2.1);
	assert.ok(plan.launchAtContextSec > 10.19, 'launch waits for the next master beat 1');
});

// REQ: LATENCY-02
test('LATENCY-02 planQuantizedLaunch skips a beat 1 inside the processor lead', () => {
	const extendedMaster = [
		...MASTER_BEATS,
		{ n: 3, bpm: 120, t: 3.0 },
		{ n: 4, bpm: 120, t: 3.5 },
		{ n: 1, bpm: 120, t: 4.0 }
	];
	const plan = ql.planQuantizedLaunch({
		nowContextTimeSec: 10,
		processorLeadSec: 0.3,
		masterPlaying: true,
		masterBeats: extendedMaster,
		masterPositionSec: 1.95,
		masterTempoRatio: 1,
		followerPlaying: false,
		followerBeats: FOLLOWER_BEATS,
		followerPositionSec: 0.3
	});
	assert.equal(plan.kind, 'armed');
	assert.ok(plan.launchAtContextSec >= 12, 'too-close beat 1 is skipped for the following downbeat');
});

// REQ: LATENCY-02
test('LATENCY-02 planQuantizedLaunch refuses without a playing master or trusted grid', () => {
	const noMaster = ql.planQuantizedLaunch({
		nowContextTimeSec: 10,
		processorLeadSec: 0.05,
		masterPlaying: false,
		masterBeats: MASTER_BEATS,
		masterPositionSec: 1.0,
		masterTempoRatio: 1,
		followerPlaying: false,
		followerBeats: FOLLOWER_BEATS,
		followerPositionSec: 0.3
	});
	assert.equal(noMaster.kind, 'refuse');
	assert.match(noMaster.reason, /QUANTIZED LAUNCH/);
});
