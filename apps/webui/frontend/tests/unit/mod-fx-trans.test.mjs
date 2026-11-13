// requirement: FXGATE-14013
// TRANS gate kernel of mod-fx-processor.js, driven offline through the real mod-fx-graph.ts (host: mod-fx-host.mjs).
import assert from 'node:assert/strict';
import { performance } from 'node:perf_hooks';
import { before, test } from 'node:test';

import { makeSignal } from '../shared/synth-signals.ts';
import { loadTypeScriptModule } from './load-typescript.mjs';
import * as H from './mod-fx-host.mjs';

const { SR, BLOCK, FLT_MIN, PROCESSOR_SOURCE, render, stereo, sine, normalise, pink, maxAbsDiff } =
	H;
const EDGES = ['hard', 'soft', 'smooth'];
const CLOSED_FLOOR = 10 ** (-90 / 20);

let G;
let lfo;
let gate;
let clickRatio;

before(async () => {
	G = await loadTypeScriptModule('src/lib/rb/mod-fx-graph.ts');
	lfo = await loadTypeScriptModule('src/lib/rb/mod-fx-lfo.ts');
	gate = await loadTypeScriptModule('src/lib/rb/trans-gate.ts');
	({ clickRatio } = await loadTypeScriptModule('src/lib/rb/slip-return-blend.ts'));
});

const makeHost = (voicing = {}) => H.makeHost(G, 'trans', voicing);
const engage = (host, opts) => H.engage(host, lfo, opts);
const dc = (seconds, amp = 0.5) => new Float32Array(Math.round(seconds * SR)).fill(amp);

/** Frames of one cut cycle, and of one edge after the trans-gate clamps, for `beats` at `bpm`. */
function cycleFrames(beats, bpm, edge, duty = 0.5) {
	const cycleSec = gate.transCycleSeconds(beats, bpm);
	const edgeFrac = Math.min(gate.edgeFracFor(edge, cycleSec), (1 - duty) / 2);
	return { T: cycleSec * SR, edge: edgeFrac * cycleSec * SR };
}

/** Linearly interpolated frames where `g` rises through 0.5. */
function risingHalfCrossings(g) {
	const out = [];
	for (let n = 1; n < g.length; n++)
		if (g[n - 1] < 0.5 && g[n] >= 0.5) out.push(n - 1 + (0.5 - g[n - 1]) / (g[n] - g[n - 1]));
	return out;
}

//----------------------------------------------------------------------------- tests

// [if] kernel trans refuses duty/edge/depthLaw, or its gain differs from transGain + transMix of trans-gate.ts on the anchor phase [then] broken, [else stop].
test('kernel trans gates with the trans-gate.ts maths, sample for sample', () => {
	const o = G.transProcessorOptions();
	assert.deepEqual([o.kernel, o.duty, o.edgeSec, o.depthLaw], ['trans', 0.5, 0.004, 'balance']);
	assert.equal(G.transProcessorOptions({ edge: 'smooth' }).edgeSec, 0.01);
	assert.throws(() => G.transProcessorOptions({ edge: 'blunt' }), /edge is unknown/);
	assert.throws(() => G.transProcessorOptions({ duty: 1.5 }), /duty must be 0..1/);
	assert.throws(() => G.transProcessorOptions({ depthLaw: 'duty' }), /depthLaw is unknown/);
	const Proc = makeHost().registered['mdt-mod-fx'];
	const options = (patch) => ({ processorOptions: { ...G.transProcessorOptions(), ...patch } });
	assert.throws(() => new Proc(options({ depthLaw: 'x' })), /depthLaw must be one of/);
	assert.throws(() => new Proc(options({ edgeSec: NaN })), /edgeSec/);
	const x = dc(0.6);
	for (const depthLaw of ['balance', 'balance_then_duty']) {
		for (const edge of EDGES) {
			for (const knob of [0.3, 0.8, 1]) {
				const host = makeHost({ edge, depthLaw, duty: 0.6 });
				assert.equal(host.nodeArgs.options.processorOptions.kernel, 'trans');
				const anchor = engage(host, { bpm: 124, cycleBeats: 1 / 4, depth: knob });
				const [out] = render(host, stereo(x));
				const edgeFrac = gate.edgeFracFor(edge, gate.transCycleSeconds(1 / 4, 124));
				const mix = gate.transMix(knob, 0.6, depthLaw);
				for (let n = 1024; n < x.length; n++) {
					const g = gate.transGain(lfo.lfoPhase(anchor, n / SR), mix.duty, edgeFrac);
					const want = 0.5 * (1 - mix.wet * (1 - g));
					assert.ok(Math.abs(out[n] - want) < 1e-6, `${depthLaw} ${edge} ${knob} @${n}`);
				}
			}
		}
	}
});

// [if] process(), the trans gain or the gate maths contain an allocating construct [then] TRANS allocates per sample, [else stop].
test('no allocation inside process() or the trans gain', () => {
	const between = (from, to) => {
		const start = PROCESSOR_SOURCE.indexOf(from);
		assert.ok(start >= 0, from);
		return PROCESSOR_SOURCE.slice(start, PROCESSOR_SOURCE.indexOf(to, start));
	};
	const bodies = [
		between('process(inputs, outputs', '\nregisterProcessor'),
		between('transGateGain(phase, duty, edgeFrac)', '\n}\n'),
		between('gain(g, phase', '\n};')
	];
	for (const body of bodies.map((b) => b.replace(/\/\/.*$/gm, ''))) {
		assert.ok(body.length > 50, 'slice found');
		for (const banned of ['new ', '=>', 'function', '= [', '= {', '...', '.slice(', '.map(']) {
			assert.equal(body.includes(banned), false, `allocating construct ${banned}`);
		}
	}
});

// [if] knob 0, effect off or no anchor changes any output sample from the input, under either depth law [then] dry is not bit-exact, [else stop].
test('knob 0, effect off and no anchor are bit-exact dry', () => {
	const x = pink(0.3);
	const cases = {
		'knob 0': (h) => engage(h, { depth: 0, cycleBeats: 1 / 4 }),
		off: (h) => (engage(h, { cycleBeats: 1 / 4 }), h.graph.setActive(false)),
		'no anchor': (h) => (h.graph.setDepth(1), h.graph.setActive(true))
	};
	for (const depthLaw of ['balance', 'balance_then_duty']) {
		for (const [name, setup] of Object.entries(cases)) {
			const host = makeHost({ edge: 'hard', depthLaw });
			setup(host);
			const out = render(host, x);
			assert.equal(maxAbsDiff(out[0], x[0]) + maxAbsDiff(out[1], x[1]), 0, `${depthLaw} ${name}`);
		}
	}
});

// [if] at 120 or 174 BPM, cut 1/4 or 1/16, the closed part is above -90 dBFS past the edge or an opening is over 1 ms off its slot [then] broken, [else stop].
test('cut period is the beat division; closed below -90 dBFS; openings on the grid', () => {
	for (const bpm of [120, 174]) {
		for (const beats of [1 / 4, 1 / 16]) {
			for (const edge of EDGES) {
				const at = `${bpm} BPM 1/${1 / beats} ${edge}`;
				const { T, edge: edgeFrames } = cycleFrames(beats, bpm, edge);
				const x = sine(1000, (4.2 * T) / SR);
				const host = makeHost({ edge, duty: 0.5, depthLaw: 'balance' });
				engage(host, { bpm, cycleBeats: beats, depth: 1 });
				const [out] = render(host, stereo(x));
				const openings = [];
				for (let k = 1; k < 4; k++) {
					const fallStart = Math.ceil((k + 0.5) * T);
					const riseStart = Math.floor((k + 1) * T - edgeFrames);
					let lastLoud = fallStart - 1;
					for (let n = fallStart; n < riseStart; n++)
						if (Math.abs(out[n]) >= CLOSED_FLOOR) lastLoud = n;
					assert.ok(lastLoud + 1 - fallStart <= edgeFrames + 1, `${at}: closed late in cycle ${k}`);
					let n = riseStart;
					while (n < out.length && !(Math.abs(x[n]) > 0.05 && out[n] / x[n] >= 0.99)) n++;
					openings.push(n);
					assert.ok(
						Math.abs(n - (k + 1) * T) <= 0.001 * SR,
						`${at}: opening ${n} vs ${(k + 1) * T}`
					);
				}
				for (let k = 1; k < openings.length; k++)
					assert.ok(Math.abs(openings[k] - openings[k - 1] - T) <= 2, `${at}: period`);
			}
		}
	}
});

// [if] a soft or smooth gate edge has click ratio over 1.5 against the dry tone [then] the edge clicks, [else stop].
test('soft and smooth gate edges: click ratio <= 1.5 at every edge', () => {
	for (const edge of ['soft', 'smooth']) {
		for (const [bpm, beats] of [
			[120, 1 / 4],
			[174, 1 / 16]
		]) {
			const { T, edge: edgeFrames } = cycleFrames(beats, bpm, edge);
			const x = sine(1000, (6 * T) / SR);
			const host = makeHost({ edge });
			engage(host, { bpm, cycleBeats: beats });
			const [out] = render(host, stereo(x));
			let edges = 0;
			for (let k = 1; k < 5; k++) {
				for (const at of [Math.round((k + 0.5) * T), Math.round((k + 1) * T - edgeFrames)]) {
					const ratio = clickRatio(out, [x], at);
					assert.ok(ratio <= 1.5, `${edge} ${bpm} BPM edge @${at}: ratio ${ratio}`);
					edges++;
				}
			}
			assert.equal(edges, 8);
		}
	}
});

// [if] any edge shape clicks over 1.5 on engage, release or a seek re-anchor with crossfade [then] broken, [else stop].
test('engage, release and re-anchor click ratio <= 1.5 for hard, soft and smooth', () => {
	const x = stereo(sine(1000, 1));
	const dry = x[0];
	const event = BLOCK * 160; // phase 0.853 of a 1-beat cycle at 120 BPM: inside the closed part
	const opts = { cycleBeats: 1 };
	for (const edge of EDGES) {
		const steady = (setup) => {
			const h = makeHost({ edge });
			setup(h);
			return render(h, x)[0];
		};
		const engaged = steady((h) => engage(h, opts));
		const on = makeHost({ edge });
		engage(on, opts);
		on.graph.setActive(false);
		const engageOut = render(on, x, { [event]: () => on.graph.setActive(true) })[0];
		assert.ok(clickRatio(engageOut, [dry, engaged], event) <= 1.5, `${edge} engage`);
		const off = makeHost({ edge });
		engage(off, opts);
		const releaseOut = render(off, x, { [event]: () => off.graph.setActive(false) })[0];
		assert.ok(clickRatio(releaseOut, [dry, engaged], event) <= 1.5, `${edge} release`);
		const seekBy = (a) => ({ ...a, beatIndex: a.beatIndex + 0.5 });
		const seek = makeHost({ edge });
		const a = engage(seek, opts);
		const relocked = render(seek, x, {
			[event]: () => (seek.graph.postAnchor(seekBy(a)), seek.graph.crossfadeWet())
		})[0];
		const after = steady((h) => h.graph.postAnchor(seekBy(engage(h, opts))));
		assert.ok(clickRatio(relocked, [engaged, after], event) <= 1.5, `${edge} re-anchor`);
	}
});

// [if] any law or edge puts a sample over 0 dBFS, a NaN, infinity or subnormal out, or the open hold is not the input sample for sample [then] broken, [else stop].
test('no clip, NaN, infinity or subnormal; open hold is the dry input with 0 latency', () => {
	const loop = makeSignal({
		kind: 'loop',
		seconds: 1,
		sampleRate: SR,
		seed: 3,
		channels: 2,
		bpm: 124
	});
	const impulse = new Float32Array(SR);
	impulse[2400] = 10 ** (-1 / 20);
	const fixtures = {
		pink: pink(1),
		loop: [loop.left, loop.right].map((c) => normalise(c)),
		impulse: stereo(impulse)
	};
	for (const [name, x] of Object.entries(fixtures)) {
		for (const edge of EDGES) {
			for (const [depthLaw, depth] of [
				['balance', 1],
				['balance_then_duty', 0.7]
			]) {
				const host = makeHost({ edge, depthLaw });
				const anchor = engage(host, { cycleBeats: 1 / 4, depth });
				const out = render(host, x);
				for (let c = 0; c < 2; c++) {
					for (let n = 0; n < x[c].length; n++) {
						const y = out[c][n];
						assert.ok(Number.isFinite(y) && Math.abs(y) <= 1, `${name} ${y}`);
						assert.ok(y === 0 || Math.abs(y) >= FLT_MIN, `${name} subnormal`);
						const phase = lfo.lfoPhase(anchor, n / SR);
						if (n > 1024 && phase > 0.01 && phase < 0.34)
							assert.equal(y, x[c][n], `${name} open @${n}`);
					}
				}
			}
		}
	}
});

// [if] a free time_ms cycle of 10, 333 or 16000 ms gates off its value by over 0.1%, or no tempo and no time_ms reports no reason [then] broken, [else stop].
test('free time_ms cycle gates at its period; no tempo and no time_ms is inert with a reason', () => {
	for (const timeMs of [10, 333, 16000]) {
		const T = (timeMs / 1000) * SR;
		const host = makeHost();
		host.graph.postAnchor(lfo.lfoTimeAnchor(timeMs, 0));
		host.graph.setDepth(1);
		host.graph.setActive(true);
		const [out] = render(host, stereo(dc(Math.max(2.1 * T, 0.2 * SR) / SR)));
		const gain = Float32Array.from(out, (y) => y / 0.5);
		const ups = risingHalfCrossings(gain).filter((u) => u > 2 * G.MOD_FX_RAMP_SEC * SR);
		assert.ok(ups.length >= 2, `${timeMs} ms: ${ups.length} openings`);
		const period = (ups.at(-1) - ups[0]) / (ups.length - 1);
		assert.ok(Math.abs(period - T) <= 0.001 * T, `${timeMs} ms: period ${period} vs ${T}`);
	}
	assert.equal(G.transInertReason(null, null), G.TRANS_NO_TEMPO_REASON);
	assert.equal(G.transInertReason(20, null), G.TRANS_NO_TEMPO_REASON);
	assert.match(
		G.TRANS_NO_TEMPO_REASON,
		/^trans is timed in beats and this deck has no tempo: load a track/
	);
	assert.equal(G.transInertReason(120, null), null);
	assert.equal(G.transInertReason(null, 16000), null);
	assert.throws(() => G.transInertReason(null, 20000), /timeMs/);
});

// [if] one TRANS instance costs over 25% of one core per second of 48 kHz stereo [then] too slow for the #9777 row, [else stop].
test('CPU per instance at 48 kHz is recorded', (t) => {
	const x = pink(10);
	const host = makeHost({ edge: 'smooth', depthLaw: 'balance_then_duty' });
	engage(host, { cycleBeats: 1 / 16, depth: 0.8 });
	render(host, [x[0].subarray(0, SR), x[1].subarray(0, SR)]);
	const t0 = performance.now();
	render(host, x);
	const msPerSec = (performance.now() - t0) / 10;
	t.diagnostic(`TRANS CPU per instance at 48 kHz stereo: ${msPerSec.toFixed(2)} ms per s of audio`);
	assert.ok(msPerSec < 250, `${msPerSec} ms per s`);
});
