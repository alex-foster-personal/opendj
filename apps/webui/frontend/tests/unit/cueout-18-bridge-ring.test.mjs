// requirement: CUEOUT-18
// [if] the cue bridge ring is primed to the target fill and fed one quantum per pull [then] fill stays at the target
// [if] the ring is empty on pull [then] output is all-zero and the underrun counter increments once
// [if] fill drifts above the band [then] exactly one frame is dropped before the next quantum, not more
// [if] fill drifts below the band [then] exactly one frame is duplicated in the quantum, not more
// [if] target fill and sample rate are known [then] bufferLatencyMs equals target/sampleRate*1000 exactly
// [if] the ring starts empty [then] pulls are silent and non-consuming until the target fill is reached
// [if] an underrun occurs after steady state [then] the ring re-enters priming before playback resumes
// [if] stereo frames are pushed [then] each output channel matches its input channel
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SAMPLE_RATE = 48000;
const QUANTUM = 128;

let ringMod;

before(async () => {
	ringMod = await loadTypeScriptModule('src/lib/player/cue-bridge-ring.ts');
});

function stereoChunk(valueL, valueR, frames = QUANTUM) {
	return {
		left: new Float32Array(frames).fill(valueL),
		right: new Float32Array(frames).fill(valueR)
	};
}

function primeToTarget(ring, target = ringMod.CUE_BRIDGE_TARGET_FRAMES) {
	const chunk = stereoChunk(0.25, -0.25);
	while (ring.fill < target) {
		ring.push(chunk.left, chunk.right);
	}
}

test('from empty, priming pulls are silent and non-consuming until the target is reached', () => {
	const ring = new ringMod.CueBridgeRing();
	const left = new Float32Array(QUANTUM);
	const right = new Float32Array(QUANTUM);
	const chunk = stereoChunk(0.5, -0.5);
	let pullsWhilePriming = 0;
	while (ring.priming) {
		const before = ring.fill;
		const result = ring.pull(left, right);
		assert.equal(result.priming, true);
		assert.ok(left.every((sample) => sample === 0));
		assert.ok(right.every((sample) => sample === 0));
		assert.equal(ring.fill, before, 'priming pull must not consume');
		// That pull may have been the one that just reached the target (it flips
		// `ring.priming` to false before returning), so pushing unconditionally
		// here would add one quantum past the target the loop is about to stop
		// on. Only push while still priming.
		if (ring.priming) ring.push(chunk.left, chunk.right);
		pullsWhilePriming += 1;
		assert.ok(pullsWhilePriming < 10_000);
	}
	assert.equal(ring.fill, ringMod.CUE_BRIDGE_TARGET_FRAMES);
	const outL = new Float32Array(QUANTUM);
	const outR = new Float32Array(QUANTUM);
	const played = ring.pull(outL, outR);
	assert.equal(played.priming, false);
	assert.notEqual(outL[0], 0);
	assert.notEqual(outR[0], 0);
});

test('steady state holds the target fill exactly once primed', () => {
	const ring = new ringMod.CueBridgeRing();
	primeToTarget(ring);
	while (ring.priming) {
		const left = new Float32Array(QUANTUM);
		const right = new Float32Array(QUANTUM);
		ring.pull(left, right);
	}
	assert.equal(ring.fill, ringMod.CUE_BRIDGE_TARGET_FRAMES);
	const left = new Float32Array(QUANTUM);
	const right = new Float32Array(QUANTUM);
	const chunk = stereoChunk(0.5, -0.5);
	for (let i = 0; i < 32; i += 1) {
		ring.push(chunk.left, chunk.right);
		ring.pull(left, right);
	}
	assert.equal(ring.fill, ringMod.CUE_BRIDGE_TARGET_FRAMES);
});

test('underrun yields silence, increments the underrun counter, and re-enters priming', () => {
	const ring = new ringMod.CueBridgeRing();
	primeToTarget(ring);
	const left = new Float32Array(QUANTUM);
	const right = new Float32Array(QUANTUM);
	while (ring.priming) {
		ring.pull(left, right);
	}
	// Drain with no input until the ring reports its underrun. Where in a quantum
	// that lands depends on how many low-fill corrections ran, so the test does not
	// assume it falls on a quantum boundary.
	let result = ring.pull(left, right);
	while (!result.underrun) {
		result = ring.pull(left, right);
	}
	assert.equal(ring.underrunCount, 1);
	assert.equal(left[QUANTUM - 1], 0, 'if the tail of the underrun quantum is not silent then stale audio plays - broken');
	assert.equal(right[QUANTUM - 1], 0);
	assert.equal(ring.priming, true);
	const next = ring.pull(left, right);
	assert.equal(next.underrun, false, 'if priming counts a second underrun then the counter overstates dropouts - broken');
	assert.equal(ring.underrunCount, 1);
	assert.ok(left.every((sample) => sample === 0));
	assert.ok(right.every((sample) => sample === 0));
});

test('after underrun, priming resumes until the target is re-earned', () => {
	const ring = new ringMod.CueBridgeRing();
	primeToTarget(ring);
	while (ring.priming) {
		const left = new Float32Array(QUANTUM);
		const right = new Float32Array(QUANTUM);
		ring.pull(left, right);
	}
	const left = new Float32Array(QUANTUM);
	const right = new Float32Array(QUANTUM);
	while (ring.fill > 0) {
		ring.pull(left, right);
	}
	// The drain above stops calling pull() the moment fill reads 0, which can
	// land exactly on a quantum boundary without ever handing the ring a pull
	// while it is ALREADY empty - that is the call that actually flips it back
	// to priming. One more pull forces that call.
	ring.pull(left, right);
	assert.equal(ring.priming, true);
	const chunk = stereoChunk(0.3, -0.3);
	while (ring.priming) {
		const before = ring.fill;
		const result = ring.pull(left, right);
		assert.equal(result.priming, true);
		assert.ok(left.every((sample) => sample === 0));
		assert.equal(ring.fill, before);
		if (ring.priming) ring.push(chunk.left, chunk.right);
	}
	assert.equal(ring.fill, ringMod.CUE_BRIDGE_TARGET_FRAMES);
});

test('fill above the band drops exactly one frame per quantum', () => {
	const ring = new ringMod.CueBridgeRing();
	primeToTarget(ring);
	while (ring.priming) {
		const left = new Float32Array(QUANTUM);
		const right = new Float32Array(QUANTUM);
		ring.pull(left, right);
	}
	const extra = stereoChunk(0.1, -0.1, QUANTUM * 2);
	ring.push(extra.left, extra.right);
	const before = ring.fill;
	const left = new Float32Array(QUANTUM);
	const right = new Float32Array(QUANTUM);
	const result = ring.pull(left, right);
	assert.equal(result.dropped, true);
	assert.equal(ring.fill, before - QUANTUM - 1);
});

test('fill below the band duplicates exactly one frame per quantum', () => {
	const ring = new ringMod.CueBridgeRing();
	primeToTarget(ring);
	while (ring.priming) {
		const left = new Float32Array(QUANTUM);
		const right = new Float32Array(QUANTUM);
		ring.pull(left, right);
	}
	const left = new Float32Array(QUANTUM);
	const right = new Float32Array(QUANTUM);
	while (ring.fill >= ringMod.CUE_BRIDGE_TARGET_FRAMES - ringMod.CUE_BRIDGE_DRIFT_BAND_FRAMES) {
		ring.pull(left, right);
	}
	assert.ok(ring.fill < ringMod.CUE_BRIDGE_TARGET_FRAMES - ringMod.CUE_BRIDGE_DRIFT_BAND_FRAMES);
	const before = ring.fill;
	const result = ring.pull(left, right);
	assert.equal(result.duplicated, true);
	assert.notEqual(left[0], 0);
	assert.notEqual(right[0], 0);
	assert.equal(left[1], left[0], 'if slot 1 is not the same frame as slot 0 then nothing was duplicated - broken');
	assert.equal(right[1], right[0]);
	assert.equal(
		ring.fill,
		before - (QUANTUM - 1),
		'if a low-fill quantum consumes a full quantum then the correction never refills the ring and it underruns - broken'
	);
});

test('stereo channels are preserved end to end', () => {
	const ring = new ringMod.CueBridgeRing();
	const leftIn = new Float32Array([0.75]);
	const rightIn = new Float32Array([-0.25]);
	while (ring.fill < ringMod.CUE_BRIDGE_TARGET_FRAMES) {
		ring.push(leftIn, rightIn);
	}
	while (ring.priming) {
		const left = new Float32Array(1);
		const right = new Float32Array(1);
		ring.pull(left, right);
	}
	const leftOut = new Float32Array(1);
	const rightOut = new Float32Array(1);
	ring.pull(leftOut, rightOut);
	assert.equal(leftOut[0], 0.75);
	assert.equal(rightOut[0], -0.25);
});

test('reported latency equals target frames over sample rate in milliseconds', () => {
	const ms = ringMod.CueBridgeRing.bufferLatencyMs(SAMPLE_RATE);
	assert.equal(ms, (ringMod.CUE_BRIDGE_TARGET_FRAMES / SAMPLE_RATE) * 1000);
});

test('a failed cue bridge start is not cached and closes its half-built context', () => {
	const source = readFileSync(new URL('../../src/lib/player/headphones.ts', import.meta.url), 'utf8');
	const body = source.slice(source.indexOf('async function _ensureCueBridge'), source.indexOf('interface _OutputSelectableMediaDevices'));
	assert.match(body, /catch \(error\) \{[\s\S]*if \(_cueBridgeReady === ready\) _cueBridgeReady = null;/,
		'if a rejected bridge start stays cached then one transient failure disables cue until reload - broken');
	assert.match(body, /catch \(error\) \{[\s\S]*await _closeCueContext\(cueContext\)[\s\S]*throw error;/,
		'if the half-built cue context is not closed on failure then every retry leaks an AudioContext - broken');
});

test('a bridge that finishes after the route tore the graph down is closed, not published', () => {
	const source = readFileSync(new URL('../../src/lib/player/headphones.ts', import.meta.url), 'utf8');
	const body = source.slice(source.indexOf('async function _ensureCueBridge'), source.indexOf('interface _OutputSelectableMediaDevices'));
	assert.match(source, /async function _closeCueContext[\s\S]*?cueContext\.close\(\)/);
	const guard = body.indexOf('_headphoneNodes !== nodes');
	assert.ok(guard > 0 && guard < body.indexOf('nodes.bridgeInput.connect(wired.bridgeSender)'),
		'if ownership is not checked before publishing then a teardown mid-start attaches the bridge to a dead graph - broken');
	assert.match(body.slice(guard), /_closeCueContext\(cueContext\)[\s\S]*?throw/,
		'if a stale bridge is not closed then every teardown race leaks an AudioContext - broken');
});

function sinkContext({ failResume = false, failRestore = false } = {}) {
	const calls = [];
	return {
		calls,
		state: 'suspended',
		sinkId: 'old-phones',
		async setSinkId(id) {
			calls.push(`sink:${id}`);
			if (failRestore && id === 'old-phones') throw new Error('restore refused');
			this.sinkId = id;
		},
		async resume() {
			calls.push('resume');
			if (failResume) throw new Error('resume refused');
			this.state = 'running';
		},
		async suspend() {
			calls.push('suspend');
			this.state = 'suspended';
		}
	};
}

test('a failed cue sink change puts the context back on the previous device', async () => {
	const headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	const ctx = sinkContext({ failResume: true });
	const holder = { cueDeviceId: 'old-phones' };
	await assert.rejects(headphones.applyCueSinkTransaction(ctx, 'new-phones', holder), /resume refused/);
	assert.equal(ctx.sinkId, 'old-phones',
		'if a failed selection leaves the live context on the new device then monitor audio plays on hardware the UI does not show - broken');
	assert.equal(holder.cueDeviceId, 'old-phones');
});

test('a cue sink that cannot be restored is silenced, not left on the wrong device', async () => {
	const headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	const ctx = sinkContext({ failResume: true, failRestore: true });
	const holder = { cueDeviceId: 'old-phones' };
	await assert.rejects(headphones.applyCueSinkTransaction(ctx, 'new-phones', holder), /resume refused/);
	assert.equal(ctx.state, 'suspended',
		'if an unrestorable context keeps running then cue audio is routed to an unpublished device - broken');
	assert.equal(holder.cueDeviceId, null);
});

test('a successful cue sink change records the new device', async () => {
	const headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	const ctx = sinkContext();
	const holder = { cueDeviceId: 'old-phones' };
	await headphones.applyCueSinkTransaction(ctx, 'new-phones', holder);
	assert.equal(ctx.sinkId, 'new-phones');
	assert.equal(ctx.state, 'running');
	assert.equal(holder.cueDeviceId, 'new-phones');
});

test('a bridge worklet that throws is reported, not left as silent output', () => {
	const wiring = readFileSync(new URL('../../src/lib/player/cue-bridge-wiring.ts', import.meta.url), 'utf8');
	assert.match(wiring, /bridgeSender\.onprocessorerror = \(\) => handlers\.onProcessorError\('sender'\)/,
		'if the sender has no processorerror handler then a thrown worklet outputs silence while liveness reads healthy - broken');
	assert.match(wiring, /bridgeReceiver\.onprocessorerror = \(\) => handlers\.onProcessorError\('receiver'\)/);
	assert.doesNotMatch(wiring, /onUnderrun\?:/, 'the handlers are required, so no caller can wire a bridge that fails silently');
});

test('a failed bridge is torn down so re-selecting the output rebuilds it', async () => {
	const headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	const calls = [];
	const nodes = {
		bridgeSender: { disconnect: () => calls.push('sender') },
		bridgeReceiver: { disconnect: () => calls.push('receiver') },
		bridgeInput: { disconnect: (target) => calls.push(target === undefined ? 'input-all' : 'input-sender') },
		cueContext: { close: async () => calls.push('close') },
		cueDeviceId: 'bt-1',
		bridgeControl: null
	};
	const message = await headphones.failCueBridge('receiver', nodes);
	assert.match(message, /receiver/);
	assert.deepEqual([nodes.bridgeSender, nodes.bridgeReceiver, nodes.cueContext, nodes.cueDeviceId], [null, null, null, null],
		'if the dead bridge stays attached then _ensureCueBridge reuses it and re-selecting never recovers - broken');
	assert.ok(calls.includes('close'), 'if the dead cue context is not closed then it leaks - broken');
	assert.ok(calls.includes('input-sender'),
		'if bridgeInput keeps its edge to the dead sender then every failure leaks a worklet node into the main graph - broken');
	assert.ok(!calls.includes('input-all'), 'disconnecting all of bridgeInput would also cut the rebuilt bridge - broken');
});

test('a failed bridge mutes the monitor gains and a new cue context stays silent until pinned', () => {
	const source = readFileSync(new URL('../../src/lib/player/headphones.ts', import.meta.url), 'utf8');
	const fail = source.match(/export async function failCueBridge[\s\S]*?\n\}\n/)[0];
	assert.match(fail, /mixerState\.headphones\.active = false;[\s\S]*?applyHeadphoneMix\(\);/,
		'if the live monitor gains are not zeroed after a bridge failure then a rebuild plays the stale mix - broken');
	const ensure = source.match(/async function _ensureCueBridge[\s\S]*?\n\}\n/)[0];
	assert.match(ensure, /new AudioContext\([^)]*\);(?:\s*\/\/[^\n]*)*\s*await cueContext\.suspend\(\);/,
		'if a new cue context runs before setSinkId pins it then monitor audio plays through the room default output - broken');
	assert.doesNotMatch(ensure, /cueContext\.resume\(\)/,
		'only the sink transaction may resume the cue context, after setSinkId lands');
});

test('a vanished cue device suspends the cue context so it cannot fall back to the room', async () => {
	const headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	const ctx = sinkContext();
	ctx.state = 'running';
	await headphones.silenceVanishedCueOutput({ cueContext: ctx });
	assert.equal(ctx.state, 'suspended',
		'if the cue context keeps running after its device vanished then Chrome can reroute monitor audio to the default output - broken');
	await headphones.silenceVanishedCueOutput({ cueContext: null });
	const source = readFileSync(new URL('../../src/lib/player/headphones.ts', import.meta.url), 'utf8');
	assert.match(source, /if \(plan\.clearCue\) \{[\s\S]*?await silenceVanishedCueOutput\(/,
		'if the vanished-device branch does not silence the cue context then the guard above is dead code - broken');
});

test('a restored cue device resumes only after its sink is applied', async () => {
	const headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	const ctx = sinkContext();
	await headphones.applyCueSinkTransaction(ctx, 'bt-1', { cueDeviceId: null });
	assert.deepEqual(ctx.calls, ['sink:bt-1', 'resume'],
		'if the context resumes before setSinkId lands then monitor audio plays on the old output meanwhile - broken');
});
