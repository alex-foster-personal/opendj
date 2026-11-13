// requirement: FXMOD-13984
// PHASER kernel of mod-fx-processor.js, driven offline through the real mod-fx-graph.ts (host: mod-fx-host.mjs).
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { makeSignal } from '../shared/synth-signals.ts';
import { loadTypeScriptModule } from './load-typescript.mjs';
import * as H from './mod-fx-host.mjs';

const { SR, BLOCK, FLT_MIN, PROCESSOR_SOURCE, render, stereo, sine, normalise, pink, maxAbsDiff } =
	H;

let G;
let lfo;
let clickRatio;

before(async () => {
	G = await loadTypeScriptModule('src/lib/rb/mod-fx-graph.ts');
	lfo = await loadTypeScriptModule('src/lib/rb/mod-fx-lfo.ts');
	({ clickRatio } = await loadTypeScriptModule('src/lib/rb/slip-return-blend.ts'));
});

const makeHost = (voicing = {}, sampleRate = SR) => H.makeHost(G, 'phaser', voicing, sampleRate);
const engage = (host, opts) => H.engage(host, lfo, opts);

/** Hold the sweep at `u` in [0, 1] on the rising half: a time anchor whose period is 1e6 s. */
function freeze(host, u) {
	const periodSec = 1e6;
	host.graph.postAnchor({ kind: 'time', contextTime: (-u / 2) * periodSec, periodSec });
	host.graph.setDepth(1);
	host.graph.setActive(true);
}

/** Lowest notch of `stages` first-order allpasses at break `fc`, 50/50 with dry: each stage at -180/stages degrees. */
const lowestNotchHz = (fc, stages) =>
	(SR / Math.PI) * Math.atan(Math.tan((Math.PI * fc) / SR) * Math.tan(Math.PI / (2 * stages)));

const peakFrom = (ch, from) => ch.subarray(from).reduce((m, x) => Math.max(m, Math.abs(x)), 0);

/** Amplitude of `hz` in `ch` over two-period windows, hop a fifth of one; returns [centreFrame, amplitude][]. */
function envelope(ch, hz) {
	const win = Math.round((2 * SR) / hz);
	const rows = [];
	for (let s = 0; s + win <= ch.length; s += win / 10) {
		let i = 0;
		let q = 0;
		for (let n = s; n < s + win; n++) {
			i += ch[n] * Math.cos((2 * Math.PI * hz * n) / SR);
			q += ch[n] * Math.sin((2 * Math.PI * hz * n) / SR);
		}
		rows.push([s + win / 2, (2 * Math.hypot(i, q)) / win]);
	}
	return rows;
}

/** Local minima under `floor`, refined by a parabola through the neighbours; returns frames. */
function dips(rows, floor) {
	const out = [];
	for (let k = 1; k < rows.length - 1; k++) {
		const [y0, y1, y2] = [rows[k - 1][1], rows[k][1], rows[k + 1][1]];
		if (y1 < floor && y1 <= y0 && y1 < y2)
			out.push(rows[k][0] + ((rows[k + 1][0] - rows[k][0]) * 0.5 * (y0 - y2)) / (y0 - 2 * y1 + y2));
	}
	return out;
}

//----------------------------------------------------------------------------- tests

// [if] kernel phaser is not registered, its stage state is not 2 x stages, or stages other than 4/8 are accepted [then] broken, [else stop].
test('kernel phaser runs PHASER_STAGES (4 default, 8 allowed) allpass sections', () => {
	for (const stages of [4, 8]) {
		const host = makeHost(stages === 4 ? {} : { stages });
		assert.deepEqual(
			[host.nodeArgs.name, host.nodeArgs.options.processorOptions.kernel],
			['mdt-mod-fx', 'phaser']
		);
		assert.equal(host.proc.state.state.length, 2 * stages);
	}
	assert.throws(() => makeHost({ stages: 6 }), /stages must be 4 or 8/);
	const Proc = makeHost().registered['mdt-mod-fx'];
	const options = (patch) => ({ processorOptions: { ...G.phaserProcessorOptions(), ...patch } });
	assert.throws(() => new Proc(options({ stages: 2 })), /stages must be 4 or 8/);
	assert.throws(() => new Proc(options({ maxHz: 30000 })), /Nyquist/);
	assert.throws(() => new Proc(options({ minHz: NaN })), /minHz/);
});

// [if] the frozen sweep at LFO trough, middle or peak puts the lowest notch off 200, 894 or 4000 Hz break (exponential) [then] broken, [else stop].
test('break frequency sweeps 200 Hz to 4 kHz exponentially along the triangle', () => {
	for (const stages of [4, 8]) {
		for (const [u, fc] of [
			[0, 200],
			[0.5, 200 * Math.sqrt(20)],
			[1, 4000]
		]) {
			const fn = lowestNotchHz(fc, stages);
			const at = (hz) => {
				const host = makeHost({ stages, feedback: 0 });
				freeze(host, u);
				return peakFrom(render(host, stereo(sine(hz, 0.3)))[0], Math.round(0.2 * SR)) / 0.5;
			};
			assert.ok(at(fn) < 0.01, `${stages} stages u=${u}: notch at ${fn.toFixed(1)} Hz`);
			assert.ok(at(fn * 2) > 0.5 && at(fn / 2) > 0.5, `${stages} stages u=${u}: notch is narrow`);
		}
	}
});

// [if] PHASER_STAGES is not 4, PHASER_FEEDBACK not 0.4, PHASER_SPREAD_DEG not 0, an override is ignored or |feedback| escapes 0.75 [then] broken, [else stop].
test('voicing constants are exported, overridable and feedback is clamped to 0.75', () => {
	assert.deepEqual(
		[G.PHASER_STAGES, G.PHASER_FEEDBACK, G.PHASER_SPREAD_DEG, G.PHASER_FEEDBACK_MAX],
		[4, 0.4, 0, 0.75]
	);
	const o = G.phaserProcessorOptions();
	assert.deepEqual([o.stages, o.feedback, o.spreadDeg, o.minHz, o.maxHz], [4, 0.4, 0, 200, 4000]);
	assert.deepEqual(
		[0.65, 2, -5].map((f) => G.phaserProcessorOptions({ feedback: f }).feedback),
		[0.65, 0.75, -0.75]
	);
	assert.equal(G.phaserProcessorOptions({ spreadDeg: 90 }).spreadDeg, 90);
	assert.equal(makeHost({ feedback: 0.65, stages: 8 }).proc.state.feedback, 0.65);
	assert.throws(() => makeHost({ feedback: NaN }), /feedback must be finite/);
});

// [if] process() or the phaser tick allocates, or state under the flush floor survives one render quantum [then] broken, [else stop].
test('state flushes subnormals once per block; no allocation inside process()', () => {
	const host = makeHost();
	const { state, last } = host.proc.state;
	state.fill(1e-31);
	last[1] = -1e-32;
	render(host, stereo(new Float32Array(BLOCK)));
	assert.equal(host.proc.state.state, state, 'stage state is never reallocated');
	assert.deepEqual([...state, ...last], new Array(state.length + 2).fill(0));
	const between = (from, to) => {
		const start = PROCESSOR_SOURCE.indexOf(from);
		return PROCESSOR_SOURCE.slice(start, PROCESSOR_SOURCE.indexOf(to, start));
	};
	const bodies = [
		between('process(inputs, outputs', '\nregisterProcessor'),
		between('tick(k,', '\n\tphaser: {'),
		between('tick(p,', '\n};'),
		between('const t = Math.tan', '\n}\n'),
		between('flushBelow(arr', '\n}\n')
	];
	for (const body of bodies.map((b) => b.replace(/\/\/.*$/gm, ''))) {
		assert.ok(body.length > 50, 'slice found');
		for (const banned of ['new ', '=>', 'function', '= [', '= {', '...', '.slice(', '.map(']) {
			assert.equal(body.includes(banned), false, `allocating construct ${banned}`);
		}
	}
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
		const host = makeHost({ feedback: 0.75, stages: 8 });
		setup(host);
		const out = render(host, x);
		assert.equal(maxAbsDiff(out[0], x[0]) + maxAbsDiff(out[1], x[1]), 0, name);
	}
});

// [if] depth 1 at feedback +-0.75, 4 or 8 stages, puts a sample over 0 dBFS or a NaN, infinity or subnormal on any fixture [then] broken, [else stop].
test('no clip, NaN, infinity or subnormal at depth 1 and feedback 0.75', () => {
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
		low: stereo(normalise(sine(90, 2))),
		impulse: stereo(impulse)
	};
	for (const [name, x] of Object.entries(fixtures)) {
		for (const voicing of [{ feedback: 0.75 }, { feedback: -0.75, stages: 8, spreadDeg: 90 }]) {
			const host = makeHost(voicing);
			engage(host, { cycleBeats: 1 });
			for (const ch of render(host, x)) {
				for (const y of ch) {
					assert.ok(Number.isFinite(y) && Math.abs(y) <= 1, `${name} ${y}`);
					assert.ok(y === 0 || Math.abs(y) >= FLT_MIN, `${name} subnormal`);
				}
			}
			for (const v of [...host.proc.state.state, ...host.proc.state.last])
				assert.ok(v === 0 || Math.abs(v) >= FLT_MIN, `${name} state subnormal`);
		}
	}
});

// [if] engage, release or a seek re-anchor has click ratio over 1.5, or the tail is above -90 dB 500 ms after release [then] broken, [else stop].
test('engage, release and re-anchor click ratio <= 1.5; tail silent after release', () => {
	const x = stereo(sine(1000, 1));
	const event = BLOCK * 200;
	const V = { feedback: 0.75 };
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
	const releaseOut = render(off, x, { [event]: () => off.graph.setActive(false) })[0];
	assert.ok(clickRatio(releaseOut, [dry, engaged], event) <= 1.5, 'release');
	const tailFrom = event + Math.round(0.5 * SR);
	assert.ok(maxAbsDiff(releaseOut, dry, tailFrom) < 10 ** (-90 / 20), 'tail below -90 dB');
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

// [if] the lowest-notch passes over a probe sine are off the division by over 0.5% or the sweep trough over 2 ms off a downbeat [then] broken, [else stop].
test('notch sweep period is the beat division and its trough sits on each downbeat', () => {
	// Probe below the second notch's range (fc x 2.41 at 4 stages, x 0.67 at 8) so only the lowest notch crosses it.
	// Trough is checked at the default 4 stages: 8 stages add about 2.4 ms of cascade lag at a 100 Hz probe.
	for (const [bpm, cycleBeats, stages, hz] of [
		[120, 4, 4, 200],
		[174, 2, 4, 200],
		[174, 2, 8, 100]
	]) {
		const host = makeHost({ stages, feedback: 0 });
		engage(host, { bpm, cycleBeats });
		const T = lfo.lfoCycleSeconds(cycleBeats, bpm) * SR;
		const [out] = render(host, stereo(sine(hz, (3 * T) / SR)));
		const d = dips(envelope(out, hz), 0.1);
		const at = `${bpm} BPM ${stages} stages`;
		assert.equal(d.length, 6, `${at}: two notch passes per cycle, got ${d.length}`);
		for (let k = 0; k + 2 < d.length; k++)
			assert.ok(Math.abs(d[k + 2] - d[k] - T) < 0.005 * T, `${at} period ${d[k + 2] - d[k]}`);
		if (stages === 8) continue;
		for (const k of [1, 3]) {
			const trough = (d[k] + d[k + 1]) / 2;
			assert.ok(Math.abs(trough - (T * (k + 1)) / 2) < 0.002 * SR, `${at} trough at ${trough}`);
		}
	}
});
