// requirement: FXMOD-13772
// FLANGER kernel of mod-fx-processor.js, driven offline: the real processor runs in a vm worklet scope
// behind the real mod-fx-graph.ts, whose AudioWorkletNode is a test-only host (inline until #10422).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import vm from 'node:vm';

import { makeSignal } from '../shared/synth-signals.ts';
import { loadTypeScriptModule } from './load-typescript.mjs';

const PROCESSOR_SOURCE = readFileSync(
	new URL('../../src/lib/rb/mod-fx-processor.js', import.meta.url),
	'utf8'
);
const SR = 48000;
const BLOCK = 128;
const FLT_MIN = 1.1754943508222875e-38;

let G;
let lfo;
let clickRatio;

before(async () => {
	G = await loadTypeScriptModule('src/lib/rb/mod-fx-graph.ts');
	lfo = await loadTypeScriptModule('src/lib/rb/mod-fx-lfo.ts');
	({ clickRatio } = await loadTypeScriptModule('src/lib/rb/slip-return-blend.ts'));
});

//----------------------------------------------------------------------------- test-only host

/** Real processor in a vm worklet scope; the graph's AudioWorkletNode forwards messages and depth to it. */
function makeHost(voicing = {}, sampleRate = SR) {
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
	host.graph = G.createModFxGraph(ctx, { kernel: 'flanger', cycleBeats: 4, timeMs: null, voicing });
	return host;
}

/** Render [left, right] in 128-frame quanta; `events[frame]()` runs before the quantum starting there. */
function render(host, input, events = {}) {
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

function grid(bpm, beats = 2000) {
	return Array.from({ length: beats }, (_u, i) => ({ n: (i % 4) + 1, bpm, t: (i * 60) / bpm }));
}

/** Engage a host at `depth` on a grid anchored at context time 0, cycle `cycleBeats`. */
function engage(host, { bpm = 120, cycleBeats = 4, depth = 1 } = {}) {
	const anchor = lfo.lfoAnchor(grid(bpm), 0, bpm, 0, cycleBeats);
	host.graph.postAnchor(anchor);
	host.graph.setDepth(depth);
	host.graph.setActive(true);
	return anchor;
}

const stereo = (mono) => [mono, mono];
const sine = (hz, seconds, amp = 0.5) =>
	Float32Array.from(
		{ length: Math.round(seconds * SR) },
		(_u, n) => amp * Math.sin((2 * Math.PI * hz * n) / SR)
	);
function pink(seconds, seed = 7) {
	const s = makeSignal({ kind: 'pink', seconds, sampleRate: SR, seed, channels: 2 });
	return [s.left, s.right].map((ch) => normalise(ch));
}
function normalise(ch, peakDb = -1) {
	const peak = ch.reduce((m, x) => Math.max(m, Math.abs(x)), 0);
	const g = 10 ** (peakDb / 20) / peak;
	return Float32Array.from(ch, (x) => x * g);
}
const maxAbsDiff = (a, b, from = 0) =>
	a.slice(from).reduce((m, x, i) => Math.max(m, Math.abs(x - b[from + i])), 0);

//----------------------------------------------------------------------------- tests

// [if] mdt-mod-fx kernel flanger sizes its delay line other than 6 ms + 2 frames, or allocates in process() [then] broken, [else stop].
test('registers mdt-mod-fx, kernel flanger, delay line allocated at construction only', () => {
	for (const rate of [44100, 48000]) {
		const host = makeHost({}, rate);
		assert.deepEqual(
			[host.nodeArgs.name, host.nodeArgs.options.processorOptions.kernel],
			['mdt-mod-fx', 'flanger']
		);
		const buf = host.proc.state.buf;
		assert.equal(buf.length, 2 * (Math.ceil(0.006 * rate) + G.FLANGER_HEADROOM_FRAMES));
		engage(host);
		render(host, pink(0.1));
		assert.equal(host.proc.state.buf, buf, 'the delay line is never reallocated');
	}
	const Proc = makeHost().registered['mdt-mod-fx'];
	const options = (patch) => ({ processorOptions: { ...G.flangerProcessorOptions(), ...patch } });
	assert.throws(() => new Proc(options({ kernel: 'chorus' })), /kernel is unknown/);
	assert.throws(() => new Proc(options({ maxDelaySec: NaN })), /maxDelaySec/);
	const process = PROCESSOR_SOURCE.slice(
		PROCESSOR_SOURCE.indexOf('process(inputs, outputs'),
		PROCESSOR_SOURCE.indexOf('\nregisterProcessor')
	);
	const tick = PROCESSOR_SOURCE.slice(
		PROCESSOR_SOURCE.indexOf('tick(k,'),
		PROCESSOR_SOURCE.indexOf('\n};')
	);
	for (const body of [process, tick].map((b) => b.replace(/\/\/.*$/gm, ''))) {
		for (const banned of [
			'new ',
			'=>',
			'function',
			'= [',
			'= {',
			'...',
			'.slice(',
			'.map(',
			'.filter('
		]) {
			assert.equal(body.includes(banned), false, `allocating construct ${banned}`);
		}
	}
});

// [if] the swept delay is off the anchor's triangle, outside 0.3..6 ms, or its trough misses a downbeat at 120/174 BPM [then] broken, [else stop].
test('LFO from the anchor message sweeps 0.3..6 ms on a triangle, trough on each downbeat', () => {
	for (const bpm of [120, 174]) {
		const host = makeHost({ feedback: 0 });
		const anchor = engage(host, { bpm, cycleBeats: 4 });
		const cycleSec = lfo.lfoCycleSeconds(4, bpm);
		const frames = Math.round(2.2 * cycleSec * SR);
		const downbeats = [1, 2].map((k) => Math.round(k * cycleSec * SR));
		const impulses = [...downbeats];
		for (let n = 960; n < frames - 600; n += 480)
			if (downbeats.every((d) => Math.abs(n - d) > 400)) impulses.push(n);
		const x = new Float32Array(frames);
		for (const n of impulses) x[n] = 1;
		const [out] = render(host, stereo(x));
		const delayAt = (n) =>
			G.FLANGER_MIN_DELAY_SEC * SR +
			(G.FLANGER_MAX_DELAY_SEC - G.FLANGER_MIN_DELAY_SEC) *
				SR *
				lfoTri(lfo.lfoPhase(anchor, n / SR));
		let lo = Infinity;
		let hi = 0;
		for (const n0 of impulses) {
			assert.equal(out[n0], 0.5, 'dry impulse lands on its own sample (0 latency)');
			let w = 0;
			let wm = 0;
			for (let m = 1; m < 300; m++) {
				w += out[n0 + m];
				wm += m * out[n0 + m];
			}
			const measured = wm / w;
			assert.ok(
				Math.abs(measured - delayAt(n0 + Math.round(measured))) < 0.1,
				`${bpm} BPM delay at ${n0}`
			);
			lo = Math.min(lo, measured);
			hi = Math.max(hi, measured);
		}
		for (const d of downbeats) {
			let wm = 0;
			for (let m = 1; m < 300; m++) wm += m * out[d + m];
			assert.ok(Math.abs(wm / 0.5 - 0.3e-3 * SR) < 0.5, `${bpm} BPM trough on downbeat ${d}`);
		}
		assert.ok(lo < 0.31e-3 * SR && lo >= 0.3e-3 * SR - 0.1, `${bpm} BPM min ${lo}`);
		assert.ok(hi > 5.9e-3 * SR && hi <= 6e-3 * SR + 0.1, `${bpm} BPM max ${hi}`);
	}
});
const lfoTri = (p) => (p < 0.5 ? 2 * p : 2 - 2 * p);

// [if] FLANGER_FEEDBACK is not 0.55, FLANGER_SPREAD_DEG not 0, an override is ignored or |feedback| escapes 0.9 [then] broken, [else stop].
test('voicing constants are exported, overridable and feedback is clamped to 0.9', () => {
	assert.equal(G.FLANGER_FEEDBACK, 0.55);
	assert.equal(G.FLANGER_SPREAD_DEG, 0);
	assert.equal(G.flangerProcessorOptions().feedback, 0.55);
	assert.deepEqual(
		[0.3, 2, -5].map((f) => G.flangerProcessorOptions({ feedback: f }).feedback),
		[0.3, 0.9, -0.9]
	);
	assert.equal(G.flangerProcessorOptions({ spreadDeg: 90 }).spreadDeg, 90);
	assert.equal(makeHost({ feedback: 0.75, spreadDeg: 45 }).proc.state.feedback, 0.75);
	assert.throws(() => makeHost({ spreadDeg: 270 }), /spreadDeg/);
});

// [if] depth 0, effect off, or no anchor yet changes any output sample from the input [then] dry is not bit-exact, [else stop].
test('depth 0, effect off and no anchor are bit-exact dry', () => {
	const x = pink(0.3);
	const cases = {
		'depth 0': (h) => engage(h, { depth: 0 }),
		off: (h) => (engage(h), h.graph.setActive(false)),
		'no anchor': (h) => (h.graph.setDepth(1), h.graph.setActive(true))
	};
	for (const [name, setup] of Object.entries(cases)) {
		const host = makeHost({ feedback: 0.9 });
		setup(host);
		const out = render(host, x);
		assert.equal(maxAbsDiff(out[0], x[0]) + maxAbsDiff(out[1], x[1]), 0, name);
	}
});

// [if] depth 1 at feedback +-0.9 puts a sample over 0 dBFS or a NaN, infinity or subnormal on any fixture [then] broken, [else stop].
test('no clip, NaN, infinity or subnormal at depth 1 and feedback 0.9', () => {
	const loop = makeSignal({
		kind: 'loop',
		seconds: 2,
		sampleRate: SR,
		seed: 3,
		channels: 2,
		bpm: 124
	});
	const impulse = new Float32Array(SR);
	impulse[2400] = 10 ** (-1 / 20);
	const fixtures = {
		pink: pink(2),
		loop: [loop.left, loop.right].map((c) => normalise(c)),
		sine: stereo(normalise(sine(441, 2))),
		impulse: stereo(impulse)
	};
	for (const [name, x] of Object.entries(fixtures)) {
		for (const voicing of [{ feedback: 0.9 }, { feedback: -0.9, spreadDeg: 90 }]) {
			const host = makeHost(voicing);
			engage(host, { cycleBeats: 1 });
			for (const ch of render(host, x)) {
				for (const y of ch) {
					assert.ok(Number.isFinite(y) && Math.abs(y) <= 1, `${name} ${y}`);
					assert.ok(y === 0 || Math.abs(y) >= FLT_MIN, `${name} subnormal`);
				}
			}
			for (const v of host.proc.state.buf)
				assert.ok(v === 0 || Math.abs(v) >= FLT_MIN, `${name} state subnormal`);
		}
	}
});

// [if] engage, release or a seek re-anchor has click ratio over 1.5, or the tail is above -90 dB 500 ms after release [then] broken, [else stop].
test('engage, release and re-anchor click ratio <= 1.5; tail silent after release', () => {
	const x = stereo(sine(1000, 1));
	const event = 128 * 200;
	const V = { feedback: 0.9 };
	const steady = (setup = engage) => {
		const h = makeHost(V);
		setup(h);
		return render(h, x)[0];
	};
	const engaged = steady();
	const dry = x[0];
	const on = makeHost(V);
	engage(on);
	on.graph.setActive(false);
	const engageOut = render(on, x, { [event]: () => on.graph.setActive(true) })[0];
	assert.ok(clickRatio(engageOut, [dry, engaged], event) <= 1.5, 'engage');
	const off = makeHost(V);
	engage(off);
	const releaseOut = render(off, x, { [event]: () => off.graph.setActive(false) });
	assert.ok(clickRatio(releaseOut[0], [dry, engaged], event) <= 1.5, 'release');
	const tailFrom = event + Math.round(0.5 * SR);
	assert.ok(
		maxAbsDiff(releaseOut[0], dry, tailFrom) < 10 ** (-90 / 20),
		'tail below -90 dB within 500 ms'
	);
	const seekBy = (a) => ({ ...a, beatIndex: a.beatIndex + 1.3 });
	const seek = makeHost(V);
	const a = engage(seek, { cycleBeats: 1 });
	const relocked = render(seek, x, {
		[event]: () => (seek.graph.postAnchor(seekBy(a)), seek.graph.crossfadeWet())
	})[0];
	const before = steady((h) => engage(h, { cycleBeats: 1 }));
	const after = steady((h) => h.graph.postAnchor(seekBy(engage(h, { cycleBeats: 1 }))));
	assert.ok(clickRatio(relocked, [before, after], event) <= 1.5, 're-anchor with crossfade');
});
