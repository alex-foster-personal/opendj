/**
 * PERF-STEMDEC-05: a stem load MOVES its PCM into the worklet instead of
 * copying it on the main thread.
 *
 * Measured Thu 1 Oct 2026 (ops/perf/stem-decode-under-playback-round-3): with
 * a deck playing, copying one bundle's eight channels (about 250 MB) made the
 * audio callback late; moving them did not. A node test cannot hear that. It
 * proves the SHAPE: which arrays are posted, whether anything was copied, and
 * that the mix, which is read again after its load, is never given up.
 *
 * Regression lines:
 *   - if a transfer load copies any channel then broken (the burst is back)
 *   - if a copy load posts the source's own channel views then broken (the mix buffer is emptied)
 *   - if a transfer the engine cannot honor moves some channels and copies others then broken
 *   - if the probe says yes for an engine that does not detach the channel then broken
 *   - if the probe swallows an error that is not the engine's refusal then broken
 *   - if stem-graph stops asking for 'transfer', or a mix load starts asking for it, then broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let handoff;

before(async () => {
	handoff = await loadTypeScriptModule('src/lib/rb/stretch-pcm-handoff.ts');
});

/** An AudioBuffer stand-in with real typed arrays and call counters. */
function pcmSource(channelViews, frames = channelViews[0].length) {
	const calls = { getChannelData: 0, copyFromChannel: 0 };
	return {
		calls,
		numberOfChannels: channelViews.length,
		length: frames,
		getChannelData(channel) {
			calls.getChannelData += 1;
			return channelViews[channel];
		},
		copyFromChannel(destination, channel) {
			calls.copyFromChannel += 1;
			destination.set(channelViews[channel].subarray(0, destination.length));
		}
	};
}

function stereo(frames = 16) {
	const left = Float32Array.from({ length: frames }, (_unused, i) => i / frames);
	const right = Float32Array.from({ length: frames }, (_unused, i) => -i / frames);
	return [left, right];
}

// REQ: PERF-STEMDEC-05
test('a transfer load posts the source channels themselves and copies nothing', () => {
	const views = stereo();
	const source = pcmSource(views);
	const { channels, handoff: happened } = handoff.pcmChannelsForWorklet(source, 'transfer', true);
	assert.equal(happened, 'transfer');
	assert.equal(channels.length, 2);
	assert.equal(channels[0], views[0], 'the posted array must BE the channel view, not a copy of it');
	assert.equal(channels[1], views[1]);
	assert.equal(source.calls.copyFromChannel, 0, 'a transfer load must not copy');
});

// REQ: PERF-STEMDEC-05
test('a copy load leaves the source attached and posts fresh arrays', () => {
	const views = stereo();
	const source = pcmSource(views);
	const { channels, handoff: happened } = handoff.pcmChannelsForWorklet(source, 'copy', true);
	assert.equal(happened, 'copy');
	assert.equal(source.calls.getChannelData, 0, 'a copy load must not even take the source views');
	assert.equal(source.calls.copyFromChannel, 2);
	for (const [index, channel] of channels.entries()) {
		assert.notEqual(channel.buffer, views[index].buffer, 'posting the source buffer would empty the mix');
		assert.deepEqual(Array.from(channel), Array.from(views[index]));
	}
});

// REQ: PERF-STEMDEC-05
test('a transfer the engine cannot honor is a whole copy and says so', () => {
	const views = stereo();
	const source = pcmSource(views);
	const { channels, handoff: happened } = handoff.pcmChannelsForWorklet(source, 'transfer', false);
	assert.equal(happened, 'copy');
	assert.equal(source.calls.copyFromChannel, 2);
	assert.ok(channels.every((channel, index) => channel.buffer !== views[index].buffer));
});

// REQ: PERF-STEMDEC-05
test('channels that are not whole private buffers are never moved, not even some of them', () => {
	const frames = 16;
	const shared = new Float32Array(frames * 2);
	const cases = {
		'two views of one buffer': [shared.subarray(0, frames), shared.subarray(frames)],
		'a view shorter than its buffer': [new Float32Array(new ArrayBuffer(frames * 8), 0, frames), new Float32Array(frames)],
		'the same view twice': (() => { const one = new Float32Array(frames); return [one, one]; })(),
		'a shared-memory channel': [new Float32Array(new SharedArrayBuffer(frames * 4)), new Float32Array(frames)]
	};
	for (const [name, views] of Object.entries(cases)) {
		assert.equal(handoff.channelsAreTransferable(views, frames), false, name);
		const source = pcmSource(views, frames);
		const { channels, handoff: happened } = handoff.pcmChannelsForWorklet(source, 'transfer', true);
		assert.equal(happened, 'copy', name);
		assert.ok(channels.every((channel) => !views.some((view) => view.buffer === channel.buffer)), `${name}: a source buffer was posted`);
	}
	// Positive control: the same check accepts the shape a real decode produces.
	assert.equal(handoff.channelsAreTransferable(stereo(frames), frames), true);
	assert.equal(handoff.channelsAreTransferable(stereo(frames), frames + 1), false, 'frame count must match');
	assert.equal(handoff.channelsAreTransferable([], frames), false, 'no channels is not a transferable bundle');
});

// REQ: PERF-STEMDEC-05
test('an unknown handoff fails instead of copying or moving', () => {
	const source = pcmSource(stereo());
	assert.throws(() => handoff.pcmChannelsForWorklet(source, 'move', true), RangeError);
	assert.equal(source.calls.getChannelData + source.calls.copyFromChannel, 0);
});

// REQ: PERF-STEMDEC-05
test('the probe says yes only when the channel really moved', () => {
	const make = () => pcmSource([new Float32Array(8)]);
	const realMove = (view) => structuredClone(view, { transfer: [view.buffer] });
	assert.equal(handoff.probeChannelTransfer(make, realMove), true, 'positive control: a real transfer detaches and carries the data');
	assert.equal(handoff.probeChannelTransfer(make, null), false, 'no structuredClone, no transfer');
	assert.equal(handoff.probeChannelTransfer(make, (view) => view.slice()), false, 'a copy that leaves the source attached is not a move');
	assert.equal(
		handoff.probeChannelTransfer(make, (view) => { structuredClone(view, { transfer: [view.buffer] }); return new Float32Array(8); }),
		false,
		'a move that loses the samples is not a move'
	);
	assert.equal(
		handoff.probeChannelTransfer(make, () => { throw new DOMException('refused', 'DataCloneError'); }),
		false,
		'the engine refusing to detach is the answer no'
	);
	assert.throws(
		() => handoff.probeChannelTransfer(make, () => { throw new TypeError('defect'); }),
		TypeError,
		'any other error is a defect and must surface'
	);
	const sharedView = new Float32Array(16).subarray(0, 8);
	let moveCalls = 0;
	assert.equal(handoff.probeChannelTransfer(() => pcmSource([sharedView]), (view) => { moveCalls += 1; return view; }), false);
	assert.equal(moveCalls, 0, 'a channel that is not a whole buffer is never offered for transfer');
});

// REQ: PERF-STEMDEC-05
test('stems give their buffers up, the mix never does', () => {
	const read = (path) => readFileSync(new URL(`../../${path}`, import.meta.url), 'utf8');
	const stemGraph = read('src/lib/rb/stem-graph.ts');
	assert.match(stemGraph, /processor\.load\(buffers\[part\] as AudioBuffer, 'transfer'\)/, 'stem loads must ask for transfer');
	const adapter = read('src/lib/rb/stretch-adapter.ts');
	assert.match(adapter, /async load\(buffer: AudioBuffer, requested: PcmHandoff = 'copy'\)/, 'a load that does not ask keeps its buffer');
	assert.match(adapter, /requested === 'transfer' && _engineMovesChannels\(this\.#context\)/, 'transfer must be gated on the probe');
	// The mix buffer is read again after its load (waveform, reloads): moving it
	// would satisfy "do not copy" completely and silence the deck's later reads.
	const mixLoads = [
		...read('src/lib/rb/audio-engine.svelte.ts').matchAll(/\.load\(([^)]*)\)\)?;/g),
		...read('src/lib/rb/deck-channel-graph.ts').matchAll(/processor\.load\(([^)]*)\)/g)
	].map((match) => match[1]).filter((args) => /buffer/i.test(args));
	assert.ok(mixLoads.length >= 3, `expected the mix load call sites, found ${mixLoads.length}`);
	for (const args of mixLoads) assert.doesNotMatch(args, /transfer/, `a mix load asks for transfer: load(${args})`);
});
