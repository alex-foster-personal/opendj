// Test-only host for mod-fx-processor.js kernels (inline until #10422): the real processor runs in a vm
// worklet scope behind the real mod-fx-graph.ts. Copied from mod-fx-flanger.test.mjs, kernel-parameterised.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

import { makeSignal } from '../shared/synth-signals.ts';

export const PROCESSOR_SOURCE = readFileSync(
	new URL('../../src/lib/rb/mod-fx-processor.js', import.meta.url),
	'utf8'
);
export const SR = 48000;
export const BLOCK = 128;
export const FLT_MIN = 1.1754943508222875e-38;

/** Real processor in a vm worklet scope; the graph's AudioWorkletNode forwards messages and depth to it. */
export function makeHost(G, kernel, voicing = {}, sampleRate = SR) {
	const registered = {};
	const scope = {
		AudioWorkletProcessor: class {
			constructor() {
				this.port = {};
			}
		},
		registerProcessor: (name, cls) => (registered[name] = cls),
		sampleRate,
		currentTime: 0
	};
	vm.runInNewContext(PROCESSOR_SOURCE, scope);
	const host = { scope, registered, depth: 0, proc: null, nodeArgs: null };
	globalThis.AudioWorkletNode = class {
		constructor(_ctx, name, options) {
			host.nodeArgs = { name, options };
			host.proc = new registered[name](options);
			this.port = { postMessage: (data) => host.proc.port.onmessage({ data }) };
			this.parameters = new Map([['depth', { setValueAtTime: (v) => (host.depth = v) }]]);
		}
	};
	const ctx = {
		get currentTime() {
			return scope.currentTime;
		}
	};
	host.graph = G.createModFxGraph(ctx, { kernel, cycleBeats: 4, timeMs: null, voicing });
	return host;
}

/** Render [left, right] in 128-frame quanta; `events[frame]()` runs before the quantum starting there. */
export function render(host, input, events = {}) {
	const frames = input[0].length;
	const out = [new Float32Array(frames), new Float32Array(frames)];
	for (let start = 0; start < frames; start += BLOCK) {
		if (events[start]) events[start]();
		const end = Math.min(frames, start + BLOCK);
		host.scope.currentTime = start / SR;
		const ins = [[input[0].subarray(start, end), input[1].subarray(start, end)]];
		const outs = [[out[0].subarray(start, end), out[1].subarray(start, end)]];
		assert.equal(host.proc.process(ins, outs, { depth: new Float32Array([host.depth]) }), true);
	}
	return out;
}

/** Engage a host at `depth` on a 4/4 grid at `bpm` anchored at context time 0, cycle `cycleBeats`. */
export function engage(host, lfo, { bpm = 120, cycleBeats = 4, depth = 1 } = {}) {
	const grid = Array.from({ length: 2000 }, (_u, i) => ({
		n: (i % 4) + 1,
		bpm,
		t: (i * 60) / bpm
	}));
	const anchor = lfo.lfoAnchor(grid, 0, bpm, 0, cycleBeats);
	host.graph.postAnchor(anchor);
	host.graph.setDepth(depth);
	host.graph.setActive(true);
	return anchor;
}

export const stereo = (mono) => [mono, mono];
export const sine = (hz, seconds, amp = 0.5) =>
	Float32Array.from(
		{ length: Math.round(seconds * SR) },
		(_u, n) => amp * Math.sin((2 * Math.PI * hz * n) / SR)
	);
export function normalise(ch, peakDb = -1) {
	const peak = ch.reduce((m, x) => Math.max(m, Math.abs(x)), 0);
	const g = 10 ** (peakDb / 20) / peak;
	return Float32Array.from(ch, (x) => x * g);
}
export function pink(seconds, seed = 7) {
	const s = makeSignal({ kind: 'pink', seconds, sampleRate: SR, seed, channels: 2 });
	return [s.left, s.right].map((ch) => normalise(ch));
}
export const maxAbsDiff = (a, b, from = 0) =>
	a.slice(from).reduce((m, x, i) => Math.max(m, Math.abs(x - b[from + i])), 0);
