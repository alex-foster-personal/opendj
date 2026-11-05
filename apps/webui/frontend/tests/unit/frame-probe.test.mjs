// PERFPROBE-01: the opt-in in-page frame probe.
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let probe;

before(async () => {
	probe = await loadTypeScriptModule('src/lib/rb/frame-probe.ts');
});

function fakeRaf() {
	let pending = null;
	let cancelled = 0;
	return {
		raf(callback) {
			pending = callback;
			return 1;
		},
		cancelRaf() {
			cancelled += 1;
			pending = null;
		},
		fire(ts) {
			const callback = pending;
			pending = null;
			callback?.(ts);
		},
		get pending() {
			return pending !== null;
		},
		get cancelled() {
			return cancelled;
		}
	};
}

test('the probe is off unless the URL or localStorage asks for it', () => {
	const empty = { getItem: () => null };
	assert.equal(probe.frameProbeRequested('', empty), false);
	assert.equal(probe.frameProbeRequested('?perfprobe=0', empty), false);
	assert.equal(probe.frameProbeRequested('?perfprobe=1', empty), true);
	assert.equal(probe.frameProbeRequested('?x=2&perfprobe=1', null), true);
	assert.equal(
		probe.frameProbeRequested('', { getItem: (k) => (k === 'mdt.perfProbe' ? '1' : null) }),
		true
	);
	assert.equal(
		probe.frameProbeRequested('', {
			getItem() {
				throw new Error('blocked storage');
			}
		}),
		false
	);
});

test('a window summarizes fps, percentiles and long frames', () => {
	const clock = { t: 0 };
	const frames = fakeRaf();
	const p = probe.startFrameProbe({
		raf: frames.raf,
		cancelRaf: frames.cancelRaf,
		now: () => clock.t,
		shell: 'tauri',
		xruns: () => 4
	});
	// First callback only anchors the clock; then 9 frames of 16 ms and one of 100 ms.
	let ts = 0;
	frames.fire(ts);
	for (let i = 0; i < 9; i += 1) frames.fire((ts += 16));
	frames.fire((ts += 100));
	clock.t = 1000;
	const w = p.take();
	assert.equal(w.shell, 'tauri');
	assert.equal(w.frames, 10);
	assert.equal(w.frames_sampled, 10);
	assert.equal(w.window_ms, 1000);
	assert.equal(w.fps, 10);
	assert.equal(w.p50_ms, 16);
	assert.equal(w.max_ms, 100);
	assert.equal(w.p99_ms, 100);
	assert.equal(w.long_frames, 1);
	assert.equal(w.xruns, 0, 'xruns are the change within the window, not the session total');
	p.stop();
});

test('take starts a fresh window', () => {
	const clock = { t: 0 };
	const frames = fakeRaf();
	const p = probe.startFrameProbe({
		raf: frames.raf,
		cancelRaf: frames.cancelRaf,
		now: () => clock.t,
		shell: 'electron',
		xruns: () => null
	});
	frames.fire(0);
	frames.fire(16);
	clock.t = 500;
	assert.equal(p.take().frames, 1);
	clock.t = 1500;
	const empty = p.take();
	assert.equal(empty.frames, 0);
	assert.equal(empty.fps, 0);
	assert.equal(empty.p50_ms, null);
	assert.equal(empty.xruns, null, 'an unarmed sentinel reports null, not a clean zero');
	p.stop();
});

test('stop cancels the frame callback', () => {
	const frames = fakeRaf();
	const p = probe.startFrameProbe({
		raf: frames.raf,
		cancelRaf: frames.cancelRaf,
		now: () => 0,
		shell: 'browser',
		xruns: () => null
	});
	assert.equal(frames.pending, true);
	p.stop();
	assert.equal(frames.cancelled, 1);
	assert.equal(frames.pending, false);
});

test('a wrapped ring keeps the frame count but samples only the ring', () => {
	const intervals = new Float64Array(probe.FRAME_PROBE_CAPACITY).fill(16);
	const w = probe.summarizeFrameWindow({
		intervals,
		kept: probe.FRAME_PROBE_CAPACITY,
		frames: probe.FRAME_PROBE_CAPACITY + 500,
		longFrames: 0,
		windowMs: 40_000,
		shell: 'browser',
		xruns: null
	});
	assert.equal(w.frames, probe.FRAME_PROBE_CAPACITY + 500);
	assert.equal(w.frames_sampled, probe.FRAME_PROBE_CAPACITY);
	assert.equal(w.p95_ms, 16);
});
