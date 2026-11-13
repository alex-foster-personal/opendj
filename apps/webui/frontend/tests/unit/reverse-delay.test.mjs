// requirement: FXECHO-14228
// [if] the processor loads [then] it registers mdt-reverse-delay, allocates only at construction and refuses out-of-range params by name, [else stop].
// [if] timeSec changes mid-window [then] the new grid starts at the next boundary and each grain has a 5 ms raised-cosine edge, [else stop].
// [if] reverseWindow gets a rising ramp [then] it writes the falling ramp sample-exactly, wrapping the ring, [else stop].
// [if] an impulse at d is rendered [then] it reappears once at 2T - d at the wet gain, clicks reverse, fb 0 leaves no second repeat, [else stop].
// [if] pink noise runs at feedback 0.95 [then] nothing exceeds 0 dBFS, is NaN, infinite or subnormal, boundaries click at most 1.5, [else stop].
// [if] the host is built [then] it has the echo slot surface, validates by name, gates the send and routes refusals, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const PROCESSOR_URL = new URL('../../src/lib/rb/reverse-delay-processor.js', import.meta.url);
const SR = 48000;
const BLOCK = 128;
const FLT_MIN = 1.1754943508222875e-38;

let proc;
let host;
let echoCurve;
let Processor;
let posted;

before(async () => {
	globalThis.sampleRate = SR;
	globalThis.AudioWorkletProcessor = class {
		constructor() {
			this.port = { postMessage: (m) => posted.push({ ...m }) };
		}
	};
	globalThis.registerProcessor = (name, cls) => name === 'mdt-reverse-delay' && (Processor = cls);
	proc = await import('../../src/lib/rb/reverse-delay-processor.js');
	({ host, echoLoopSoftClipCurve: echoCurve } = await loadTypeScriptModule(
		'tests/unit/fixtures/reverse-delay-entry.ts'
	));
	assert.ok(Processor, 'if mdt-reverse-delay is not registered then broken');
});

function newProcessor() {
	posted = [];
	return new Processor({ processorOptions: host.reverseDelayProcessorOptions() });
}

/** Render stereo in 128-frame quanta; `paramsAt(frame)` gives the k-rate params of that quantum. */
function render(p, frames, sourceAt, paramsAt) {
	const out = [new Float32Array(frames), new Float32Array(frames)];
	const inL = new Float32Array(BLOCK);
	const params = {
		timeSec: new Float32Array(1),
		feedback: new Float32Array(1),
		wet: new Float32Array(1)
	};
	for (let start = 0; start < frames; start += BLOCK) {
		const n = Math.min(BLOCK, frames - start);
		for (let i = 0; i < n; i++) inL[i] = sourceAt(start + i);
		const values = { timeSec: 0.25, feedback: 0, wet: 1, ...paramsAt(start) };
		for (const name of Object.keys(params)) params[name][0] = values[name];
		const outputs = [[out[0].subarray(start, start + n), out[1].subarray(start, start + n)]];
		assert.equal(p.process([[inL.subarray(0, n), inL.subarray(0, n)]], outputs, params), true);
	}
	return out;
}

function pinkNoise(frames, seed = 10006) {
	let s = seed;
	let [b0, b1, b2] = [0, 0, 0];
	const x = new Float32Array(frames);
	let peak = 0;
	for (let i = 0; i < frames; i++) {
		s ^= s << 13;
		s ^= s >>> 17;
		s ^= s << 5;
		const w = ((s >>> 0) / 4294967296) * 2 - 1;
		b0 = 0.99765 * b0 + w * 0.099046;
		b1 = 0.963 * b1 + w * 0.2965164;
		b2 = 0.57 * b2 + w * 1.0526913;
		x[i] = b0 + b1 + b2 + w * 0.1848;
		peak = Math.max(peak, Math.abs(x[i]));
	}
	return x.map((v) => (v / peak) * 0.99);
}

const db = (ratio) => 20 * Math.log10(ratio);
const maxAbs = (x, from = 0, to = x.length) =>
	x.subarray(from, to).reduce((m, v) => Math.max(m, Math.abs(v)), 0);

/** Max |y[n]-y[n-1]| across a boundary's edge region over the max inside the grain bodies either side. */
function clickRatio(y, at, edge, len) {
	const slope = (from, to) => {
		let m = 0;
		for (let n = from; n < to; n++) m = Math.max(m, Math.abs(y[n] - y[n - 1]));
		return m;
	};
	const body = Math.max(
		slope(at - len + edge + 64, at - edge - 64),
		slope(at + edge + 64, at + len - edge - 64)
	);
	return slope(at - edge - 8, at + edge + 8) / body;
}

//--------------------------------------------------------------------- processor

describe('processor', () => {
	test('registers, allocates the 4 s stereo ring and scratch at construction only, refuses by name', () => {
		const p = newProcessor();
		const descriptors = Processor.parameterDescriptors;
		assert.deepEqual(
			descriptors.map((d) => [d.name, d.defaultValue, d.automationRate, 'minValue' in d]),
			[
				['timeSec', 0.5, 'k-rate', false],
				['feedback', 0, 'k-rate', false],
				['wet', 1, 'k-rate', false]
			]
		);
		assert.equal(proc.REVERSE_DELAY_PROCESSOR_NAME, host.REVERSE_DELAY_PROCESSOR_NAME);
		assert.deepEqual(
			JSON.parse(JSON.stringify(proc.REVERSE_DELAY_PARAMS)),
			host.REVERSE_DELAY_PARAMS
		);
		const buffers = [...p.ring, ...p.scratch];
		assert.deepEqual(
			buffers.map((b) => b.length),
			Array(4).fill(Math.ceil(4 * SR))
		);
		const times = [0.25, 0.25, 4, 0.0027, 9, Number.NaN, 0.1];
		render(
			p,
			20 * SR,
			(i) => Math.sin(i / 7) * 0.3,
			(f) => ({ timeSec: times[Math.floor(f / (3 * SR))] })
		);
		assert.ok(
			[...p.ring, ...p.scratch].every((b, k) => b === buffers[k]),
			'if a buffer was replaced then broken'
		);
		const src = readFileSync(PROCESSOR_URL, 'utf8');
		assert.ok(!/\bimport\b/.test(src), 'if the processor imports then broken');
		const hot = src.slice(src.indexOf('\t_readParams('), src.indexOf('\nregisterProcessor'));
		assert.doesNotMatch(
			hot.replace(/\/\/.*$/gm, ''),
			/new |=>|function|= \[|= \{|\.\.\.|\.slice\(|\.map\(/
		);
		assert.deepEqual(
			posted.map((m) => [m.reason, m.param, Number.isNaN(m.value) ? 'NaN' : m.value]),
			[
				[proc.REVERSE_DELAY_REFUSAL, 'timeSec', 9],
				[proc.REVERSE_DELAY_REFUSAL, 'timeSec', 'NaN']
			]
		);
		const q = newProcessor();
		const y = render(
			q,
			SR,
			(i) => (i === 1000 ? 0.4 : 0),
			(f) => ({ timeSec: 0.25, feedback: f < 6000 ? 0.5 : 1.2 })
		);
		assert.deepEqual(
			posted.map((m) => [m.param, Math.fround(1.2) === m.value]),
			[['feedback', true]]
		);
		const second = 2 * 12000 + 1000;
		assert.ok(
			Math.abs(y[0][second] - 0.2) < 1e-6,
			`if feedback was clamped instead of held then broken (${y[0][second]})`
		);
	});

	test('a time change lands at the next boundary, and each grain carries the 5 ms raised-cosine edge', () => {
		assert.equal(proc.REVERSE_GRAIN_EDGE_SEC, 0.005);
		const p = newProcessor();
		const clicks = new Map([
			[8000, 0.3],
			[10600, 0.2]
		]);
		const y = render(
			p,
			16000,
			(i) => clicks.get(i) ?? 0,
			(f) => ({ timeSec: f < 7000 ? 0.1 : 0.05 })
		);
		const hits = [...y[0].keys()].filter((i) => Math.abs(y[0][i]) > 1e-6);
		assert.deepEqual(hits, [9600 + (4800 - 1 - 8000 + 4800), 12000 + (2400 - 1 - 1000)]);
		const dc = render(
			newProcessor(),
			3 * 4800,
			() => 0.3,
			() => ({ timeSec: 0.1 })
		)[0];
		const e = Math.round(0.005 * SR);
		const want = (k) => 0.3 * (0.5 - 0.5 * Math.cos((Math.PI * (k + 0.5)) / e));
		for (const k of [0, 60, 120, 239]) {
			assert.ok(Math.abs(dc[4800 + k] - want(k)) < 1e-6, `fade-in at ${k}`);
			assert.ok(Math.abs(dc[2 * 4800 - 1 - k] - want(k)) < 1e-6, `fade-out at ${k}`);
		}
		assert.ok(Math.abs(dc[4800 + e] - 0.3) < 1e-6);
	});

	test('reverseWindow turns a rising ramp into a falling ramp sample-exactly, wrapping the ring', () => {
		const ring = Float32Array.from({ length: 128 }, (_, i) => (i + 28) % 128);
		const out = new Float32Array(64);
		proc.reverseWindow(ring, 100, 50, out);
		assert.deepEqual(
			[...out.subarray(0, 50)],
			Array.from({ length: 50 }, (_, j) => 49 - j)
		);
		assert.throws(() => proc.reverseWindow(ring, 0, 65, out), /reverseWindow len 65/);
	});
});

//--------------------------------------------------------------------- renders

describe('render', () => {
	test('an impulse at d returns once at 2T - d at the wet gain; four clicks come back reversed; fb 0 has no second repeat', () => {
		const T = 12000;
		const d = 3000;
		const y = render(
			newProcessor(),
			4 * T,
			(i) => (i === d ? 0.4 : 0),
			() => ({ wet: 0.7 })
		)[0];
		const hits = [...y.keys()].filter((i) => Math.abs(y[i]) > 1e-9);
		assert.equal(hits.length, 1, 'if the impulse comes back more or less than once then broken');
		assert.ok(Math.abs(hits[0] - (2 * T - d)) <= 1);
		assert.ok(Math.abs(db(y[hits[0]] / (0.4 * 0.7))) < 0.5);
		assert.ok(maxAbs(y, 2 * T) === 0 || db(maxAbs(y, 2 * T) / 0.4) < -90);
		const train = new Map([
			[2000, 0.1],
			[4000, 0.2],
			[6000, 0.3],
			[8000, 0.4]
		]);
		const z = render(
			newProcessor(),
			2 * T,
			(i) => train.get(i) ?? 0,
			() => ({})
		)[0];
		const back = [...z.keys()]
			.filter((i) => Math.abs(z[i]) > 1e-9)
			.map((i) => [i, Math.fround(z[i])]);
		assert.deepEqual(
			back,
			[8000, 6000, 4000, 2000].map((at) => [2 * T - 1 - at, Math.fround(train.get(at))])
		);
		const fb = render(
			newProcessor(),
			3 * T,
			(i) => (i === d ? 0.4 : 0),
			() => ({ feedback: 0.5 })
		)[0];
		assert.ok(
			Math.abs(db(fb[2 * T + d] / 0.2)) < 0.5,
			'if feedback does not give a forward second repeat then broken'
		);
	});

	test('pink noise at feedback 0.95: bounded, finite, no subnormal, boundaries click at most 1.5, no dry leak', () => {
		const T = 0.25 * SR;
		const noise = pinkNoise(20 * SR);
		const y = render(
			newProcessor(),
			20 * SR,
			(i) => noise[i],
			() => ({ feedback: 0.95 })
		);
		for (const ch of y) {
			assert.ok(ch.every((v) => Number.isFinite(v) && !(v !== 0 && Math.abs(v) < FLT_MIN)));
			assert.ok(maxAbs(ch) < 1, `if a sample reaches 0 dBFS then broken (${maxAbs(ch)})`);
		}
		assert.equal(
			maxAbs(y[0], 0, T),
			0,
			'if the first window carries dry signal then the kernel is not parallel'
		);
		const sine = (i) => 0.3 * Math.sin((2 * Math.PI * 437 * i) / SR);
		const s = render(newProcessor(), 3 * SR, sine, () => ({ feedback: 0.5 }))[0];
		const e = Math.round(0.005 * SR);
		for (let at = 2 * T; at < 3 * SR - T; at += T) {
			assert.ok(clickRatio(s, at, e, T) <= 1.5, `sine boundary ${at}`);
			assert.ok(clickRatio(y[0], at, e, T) <= 1.5, `pink boundary ${at}`);
		}
		const tail = newProcessor();
		const quiet = render(
			tail,
			12 * SR,
			(i) => (i < SR ? noise[i] : 0),
			() => ({ timeSec: 0.0027, feedback: 0.95 })
		);
		for (const ch of [...quiet, ...tail.ring])
			assert.ok(ch.every((v) => v === 0 || Math.abs(v) >= FLT_MIN));
	});

	test('the loop soft clip is the #9813 echo curve', () => {
		const curve = echoCurve();
		const { clipKnee, clipCeiling } = host.reverseDelayProcessorOptions();
		for (let k = 0; k < curve.length; k += 16) {
			const x = (2 * k) / (curve.length - 1) - 1;
			assert.ok(
				Math.abs(proc.reverseDelaySoftClip(x, clipKnee, clipCeiling) - curve[k]) < 1e-6,
				`x ${x}`
			);
		}
	});
});

//--------------------------------------------------------------------- host

describe('host', () => {
	const fakeParam = () => ({
		value: 1,
		events: [],
		cancelScheduledValues(t) {
			this.events.push(['cancel', t]);
		},
		setValueAtTime(v, t) {
			this.events.push(['set', v, t]);
		},
		linearRampToValueAtTime(v, t) {
			this.events.push(['ramp', v, t]);
		}
	});
	const fakeNode = (log, id) => ({
		id,
		connect: (to) => log.push([id, '->', to.id]),
		disconnect: () => log.push([id, 'x'])
	});

	test('the echo slot surface, by-name validation, the send gate, refusal routing and dispose', async () => {
		const log = [];
		const refusals = [];
		let built;
		let gains = 0;
		const ctx = { createGain: () => ({ ...fakeNode(log, `gain${gains++}`), gain: fakeParam() }) };
		const kernel = host.buildReverseDelayKernel(ctx, {
			createWorkletNode: (c, name, options) => {
				built = { name, options };
				const parameters = new Map(['timeSec', 'feedback', 'wet'].map((n) => [n, fakeParam()]));
				return { ...fakeNode(log, 'worklet'), port: {}, parameters };
			},
			onRefusal: (r) => refusals.push(r)
		});
		for (const key of ['input', 'output', 'setTime', 'setFeedback', 'gateInput', 'dispose'])
			assert.ok(key in kernel, key);
		assert.equal(built.name, 'mdt-reverse-delay');
		new Processor({ processorOptions: built.options.processorOptions });
		kernel.setTime(0.5, 2);
		kernel.setFeedback(0.95, 3);
		assert.throws(() => kernel.setTime(4.5, 4), /reverse delay timeSec must be 0.0027..4; got 4.5/);
		assert.throws(() => kernel.setFeedback(-0.1, 4), /feedback/);
		assert.deepEqual(kernel.node.parameters.get('timeSec').events, [['set', 0.5, 2]]);
		kernel.gateInput(false, 5);
		assert.deepEqual(kernel.input.gain.events, [
			['cancel', 5],
			['set', 1, 5],
			['ramp', 0, 5 + host.REVERSE_DELAY_GATE_RAMP_SEC]
		]);
		kernel.node.port.onmessage({ data: { type: 'refusal', param: 'wet', value: 2 } });
		assert.deepEqual(
			refusals.map((r) => r.param),
			['wet']
		);
		kernel.dispose();
		assert.deepEqual(log, [
			['gain0', '->', 'worklet'],
			['worklet', '->', 'gain1'],
			['gain0', 'x'],
			['worklet', 'x'],
			['gain1', 'x']
		]);
		const urls = [];
		const audioCtx = { audioWorklet: { addModule: async (url) => void urls.push(url) } };
		await Promise.all([
			host.loadReverseDelayWorklet(audioCtx),
			host.loadReverseDelayWorklet(audioCtx)
		]);
		assert.equal(urls.length, 1);
		assert.match(urls[0], /reverse-delay-processor\.js$/);
	});
});
