// requirement: SNDCOL-10655
// [if] the knob turns counterclockwise on CRUSH [then] bits fall 16 -> 4 and decimation rises 1 -> 64 on the exponential taper
// [if] the knob turns clockwise on CRUSH [then] the crush holds at CRUSH_CW_FIXED_AMOUNT and the highpass rises 20 Hz -> 8 kHz (log)
// [if] PARAMETER is 0.5 [then] the depth curve is exactly the calibrated one
// [if] the worklet processor runs in node [then] it equals the pure TS mirror sample for sample
// [if] a left-only signal is crushed [then] the right output is exactly silent
// [if] process() runs [then] it allocates nothing
// [if] a processor option is missing or out of range [then] construction throws naming it
// [if] the knob sweeps 0 -> 1 -> 0 in 100 ms, or sits anywhere on the COLOR x PARAMETER grid [then] no NaN, infinity, subnormal or sample over 0 dBFS
// [if] the CCW sweep runs on a sine [then] the level stays within 1.5 dB of dry (no make-up gain needed)
// [if] a channel first selects CRUSH [then] its worklet module loads once per context, and never while every channel is elsewhere
// [if] the worklet is loading or failed to load [then] the channel plays dry and inert_reason says why
// [if] the crush processor raises processorerror [then] inert_reason names CRUSH_UNAVAILABLE, the wet closes, filterDry returns to 1 and reselecting never rebuilds the node
// [if] the knob leaves or returns to center on CRUSH [then] filterDry and the wet path crossfade linearly over CRUSH_WET_CROSSFADE_SEC
// [if] an agent sends color_fx effect=crush [then] the dispatcher accepts it, the mirror reports it, and Rust mode refuses it by name
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, beforeEach, describe, test } from 'node:test';
import vm from 'node:vm';

import { loadTypeScriptModule } from './load-typescript.mjs';

const PROCESSOR_PATH = new URL('../../src/lib/rb/color-fx-crush-processor.js', import.meta.url);
const SR = 48000;
const BLOCK = 128;
const FLT_MIN = 1.1754943508222875e-38;
/** RMS drift allowed across the CCW sweep with no make-up gain (dB). */
const CRUSH_LOUDNESS_TOLERANCE_DB = 1.5;

let crush;
let pure;
let live;
let channel;
let ipc;
let rust;

//--------------------------------------------------------------------- inline worklet host
// Until the shared node host (#10422) lands: run the processor file in a vm
// sandbox with stub AudioWorkletProcessor / registerProcessor / sampleRate.

function loadProcessorClass(sampleRate = SR) {
	const registered = {};
	const sandbox = {
		AudioWorkletProcessor: class {
			constructor() {
				this.port = { postMessage() {} };
			}
		},
		registerProcessor: (name, cls) => {
			registered[name] = cls;
		},
		sampleRate,
		Math,
		Float64Array,
		Float32Array,
		Number,
		RangeError,
		String
	};
	vm.runInNewContext(readFileSync(PROCESSOR_PATH, 'utf8'), sandbox);
	return registered['mdt-color-fx-crush'];
}

function newProcessor(voicing, overrides = {}) {
	const Cls = loadProcessorClass();
	return new Cls({ processorOptions: { ...crush.crushProcessorOptions(voicing), ...overrides } });
}

/** Drive the processor over whole quanta, as the audio thread does. */
function runProcessor(proc, input, travel, parameter) {
	const frames = input[0].length;
	const out = [new Float32Array(frames), new Float32Array(frames)];
	const param = new Float32Array([parameter]);
	for (let start = 0; start < frames; start += BLOCK) {
		const end = Math.min(frames, start + BLOCK);
		const channels = input.filter((c) => c !== null).map((c) => c.subarray(start, end));
		const outputs = [[out[0].subarray(start, end), out[1].subarray(start, end)]];
		const t = travel.length === 1 ? travel : travel.subarray(start, end);
		assert.equal(proc.process([channels], outputs, { travel: t, parameter: param }), true);
	}
	return out;
}

//--------------------------------------------------------------------- fixtures

function noiseSine(frames, peak = 10 ** (-3 / 20)) {
	let state = 0x9805;
	const next = () => {
		state ^= state << 13;
		state ^= state >>> 17;
		state ^= state << 5;
		return ((state >>> 0) / 0xffffffff) * 2 - 1;
	};
	const l = new Float32Array(frames);
	const r = new Float32Array(frames);
	for (let i = 0; i < frames; i++) {
		const sine = Math.sin((2 * Math.PI * 440 * i) / SR);
		l[i] = peak * (0.5 * sine + 0.5 * next());
		r[i] = peak * (0.5 * sine + 0.5 * next());
	}
	return [l, r];
}

function sine(frames, hz = 440, peak = 10 ** (-3 / 20)) {
	const s = new Float32Array(frames);
	for (let i = 0; i < frames; i++) s[i] = peak * Math.sin((2 * Math.PI * hz * i) / SR);
	return s;
}

/** Travel automation across CCW, the detent and CW. */
function travelSweep(frames) {
	const t = new Float32Array(frames);
	for (let i = 0; i < frames; i++)
		t[i] = Math.sin((2 * Math.PI * i) / frames) * (i % 7000 < 1500 ? 0 : 1);
	return t;
}

function rms(a) {
	let s = 0;
	for (const x of a) s += x * x;
	return Math.sqrt(s / a.length);
}

before(async () => {
	({ crush, pure, live, channel, ipc, rust } = await loadTypeScriptModule(
		'tests/unit/fixtures/color-fx-entry.ts',
		{
			viteApiBase: 'http://127.0.0.1:9'
		}
	));
});

after(() => {
	live._resetColorFxForTests();
});

//--------------------------------------------------------------------- laws

describe('laws', () => {
	test('CCW: decimation 1 -> 64 exponential, bits 16 -> 4 linear', () => {
		assert.equal(crush.crushDecimationFor(0), 1);
		assert.equal(crush.crushDecimationFor(1), 64);
		assert.equal(crush.crushDecimationFor(0.5), 8, 'exponential midpoint');
		assert.equal(crush.crushBitsFor(0), 16);
		assert.equal(crush.crushBitsFor(1), 4);
		assert.equal(crush.crushBitsFor(0.5), 10);
		assert.ok(
			crush.crushDecimationFor(0.5, 'steep') > 8 && crush.crushDecimationFor(0.5, 'late') < 8
		);
	});

	test('PARAMETER 0.5 is the curve: |travel| CCW, the fixed amount CW', () => {
		assert.equal(crush.CRUSH_CW_FIXED_AMOUNT, 0.6);
		for (const t of [0.1, 0.4, 1]) {
			assert.equal(crush.crushAmountFor(-t, 0.5), t);
			assert.equal(crush.crushAmountFor(t, 0.5), crush.CRUSH_CW_FIXED_AMOUNT);
		}
		assert.equal(crush.crushAmountFor(0, 0.5), 0);
		assert.equal(crush.crushAmountFor(-0.4, 0), 0, 'PARAMETER 0 is clean');
		assert.equal(crush.crushAmountFor(-0.5, 1), 1, 'PARAMETER 1 reaches full crush at half travel');
	});

	test('HPF: 20 Hz CCW and at the detent, log to 8 kHz CW', () => {
		assert.equal(crush.crushHighpassHzFor(-1), 20);
		assert.equal(crush.crushHighpassHzFor(0), 20);
		assert.ok(Math.abs(crush.crushHighpassHzFor(1) - 8000) < 1e-9);
		assert.ok(Math.abs(crush.crushHighpassHzFor(0.5) - 400) < 1e-9, 'log midpoint sqrt(20*8000)');
	});

	test('targets: wet only outside the detent, signed travel from the shared detent law', () => {
		assert.equal(crush.crushTargets(0.5, 0.5).wet, 0);
		assert.equal(crush.crushTargets(0.505, 0.5).wet, 0);
		assert.equal(crush.crushTargets(0.15, 0.5).wet, 1);
		assert.ok(crush.crushTargets(0.15, 0.5).travel < 0);
		assert.ok(crush.crushTargets(0.85, 0.5).travel > 0);
		assert.equal(crush.crushTargets(0, 0.5).travel, -1);
		assert.equal(crush.crushTargets(1, 0.5).travel, 1);
	});

	test('the pure color targets for CRUSH are the FILTER bypass with every send closed', () => {
		const bypass = pure.colorFxTargets('filter', 0.5, false).filter;
		for (const v of [0, 0.15, 0.5, 0.85, 1]) {
			const t = pure.colorFxTargets('crush', v, true);
			assert.deepEqual(t.filter, bypass);
			assert.equal(t.darkSend + t.brightSend + t.echoIn + t.spaceIn, 0);
		}
	});
});

//--------------------------------------------------------------------- processor

describe('processor', () => {
	test('equals the TS mirror sample for sample (a-rate and k-rate travel, PARAMETER 0, 0.5, 1)', () => {
		const input = noiseSine(SR);
		const sweep = travelSweep(SR);
		for (const parameter of [0, 0.5, 1]) {
			for (const travel of [sweep, new Float32Array([-0.7]), new Float32Array([0.6])]) {
				for (const taper of ['exponential', 'steep', 'late']) {
					const voicing = { taper, cwFixedAmount: 0.6 };
					const got = runProcessor(newProcessor(voicing), input, travel, parameter);
					const want = crush.renderCrush(input, travel, parameter, SR, voicing);
					assert.deepEqual(got[0], want[0], `L p=${parameter} len=${travel.length} ${taper}`);
					assert.deepEqual(got[1], want[1], `R p=${parameter} len=${travel.length} ${taper}`);
				}
			}
		}
		// Control: the crush actually changes the signal.
		const crushed = crush.renderCrush(input, new Float32Array([-1]), 0.5, SR);
		assert.ok(crushed[0].some((x, i) => x !== input[0][i]));
	});

	test('hold state is per channel: a left-only input leaves the right output exactly silent', () => {
		const [l] = noiseSine(SR / 4);
		for (const value of [0.15, 0.85]) {
			const travel = new Float32Array([crush.crushTargets(value, 0.5).travel]);
			const [outL, outR] = runProcessor(newProcessor(), [l, null], travel, 0.5);
			assert.ok(
				outR.every((x) => x === 0),
				`right silent at ${value}`
			);
			assert.ok(
				outL.some((x) => x !== 0),
				'left plays (control)'
			);
			const [outL2, outR2] = runProcessor(
				newProcessor(),
				[l, new Float32Array(l.length)],
				travel,
				0.5
			);
			assert.deepEqual(outL2, outL);
			assert.ok(outR2.every((x) => x === 0));
		}
	});

	test('process() allocates nothing; held state is sized in the constructor', () => {
		const src = readFileSync(PROCESSOR_PATH, 'utf8');
		const start = src.indexOf('process(inputs, outputs, parameters) {');
		const end = src.indexOf('\nregisterProcessor');
		assert.ok(start > 0 && end > start);
		const body = src
			.slice(start + 'process(inputs, outputs, parameters) {'.length, end)
			.replace(/\/\/.*$/gm, '');
		for (const banned of [
			'new ',
			'=>',
			'function',
			'= [',
			'= {',
			'...',
			'.slice(',
			'.map(',
			'.concat(',
			'.filter('
		]) {
			assert.equal(body.includes(banned), false, `process() contains ${banned}`);
		}
		assert.ok(/this\.held = new Float64Array\(2\)/.test(src));
	});

	test('construction fails fast on a missing or out-of-range option', () => {
		assert.throws(
			() => newProcessor(undefined, { maxBits: undefined }),
			(e) => e.name === 'RangeError' && /maxBits/.test(e.message)
		);
		assert.throws(
			() => newProcessor(undefined, { minBits: 1 }),
			(e) => e.name === 'RangeError' && /minBits/.test(e.message)
		);
		assert.throws(
			() => newProcessor(undefined, { minDecimation: 0.5 }),
			(e) => e.name === 'RangeError' && /minDecimation/.test(e.message)
		);
		assert.throws(
			() => newProcessor(undefined, { taper: 'wobbly' }),
			(e) => e.name === 'RangeError' && /taper/.test(e.message)
		);
		assert.ok(newProcessor() !== null, 'the shipped options construct (control)');
	});

	test('finite and under 0 dBFS across the COLOR x PARAMETER grid and a fast 0 -> 1 -> 0 sweep', () => {
		const input = noiseSine(SR / 4);
		const check = (out, label) => {
			for (const ch of out) {
				for (const x of ch) {
					const a = Math.abs(x);
					if (a <= 1 && (x === 0 || a >= FLT_MIN)) continue; // NaN fails both comparisons
					assert.ok(Number.isFinite(x), `${label}: non-finite`);
					assert.ok(x === 0 || a >= FLT_MIN, `${label}: subnormal`);
					assert.fail(`${label}: over 0 dBFS (${x})`);
				}
			}
		};
		const gridInput = input.map((ch) => ch.subarray(0, 2048)); // >= 32 holds at decimation 64
		for (let c = 0; c <= 20; c++) {
			for (let p = 0; p <= 20; p++) {
				const travel = new Float32Array([crush.crushTargets(c / 20, p / 20).travel]);
				check(
					runProcessor(newProcessor(), gridInput, travel, p / 20),
					`color ${c / 20} param ${p / 20}`
				);
			}
		}
		const sweepFrames = Math.round(SR * 0.1);
		const travel = new Float32Array(input[0].length);
		for (let i = 0; i < sweepFrames; i++) {
			const color = i < sweepFrames / 2 ? (2 * i) / sweepFrames : 2 - (2 * i) / sweepFrames;
			travel[i] = crush.crushTargets(color, 0.5).travel;
		}
		for (const parameter of [0, 1])
			check(runProcessor(newProcessor(), input, travel, parameter), `sweep p=${parameter}`);
	});

	test('no make-up gain: the CCW sweep stays within the loudness tolerance of dry', () => {
		const s = sine(SR / 2);
		const dry = rms(s);
		for (const color of [0.45, 0.35, 0.25, 0.15, 0.05, 0]) {
			const travel = new Float32Array([crush.crushTargets(color, 0.5).travel]);
			const [out] = runProcessor(newProcessor(), [s, s], travel, 0.5);
			const settled = out.subarray(SR / 10);
			const db = 20 * Math.log10(rms(settled) / rms(s.subarray(SR / 10)));
			assert.ok(
				Math.abs(db) <= CRUSH_LOUDNESS_TOLERANCE_DB,
				`color ${color}: ${db.toFixed(2)} dB vs dry ${dry}`
			);
		}
	});
});

//--------------------------------------------------------------------- live module

class FakeParam {
	constructor(value = 0) {
		this.value = value;
		this.events = [];
	}
	cancelScheduledValues(t) {
		this.events = this.events.filter((e) => e[2] < t);
	}
	setValueAtTime(v, t) {
		this.events.push(['set', v, t]);
	}
	setTargetAtTime(v, t, tau) {
		this.events.push(['target', v, t, tau]);
	}
	linearRampToValueAtTime(v, t) {
		this.events.push(['linear', v, t]);
	}
	landing() {
		let out = this.value;
		for (const e of this.events) out = e[1];
		return out;
	}
}
class FakeNode {
	constructor(kind, ctx) {
		this.kind = kind;
		Object.defineProperty(this, 'context', { value: ctx, enumerable: false });
		this.outputs = new Set();
	}
	connect(node) {
		this.outputs.add(node);
		return node;
	}
	disconnect(node) {
		if (node === undefined) this.outputs.clear();
		else this.outputs.delete(node);
	}
}
function fakeContext(addModule) {
	const ctx = {
		currentTime: 10,
		sampleRate: 8000,
		audioWorklet: { addModule },
		_node(kind) {
			const n = new FakeNode(kind, ctx);
			ctx.created.push(n);
			return n;
		},
		createGain() {
			const n = ctx._node('gain');
			n.gain = new FakeParam(1);
			return n;
		},
		createAnalyser() {
			return ctx._node('analyser');
		},
		createDelay(max) {
			const n = ctx._node('delay');
			n.maxDelayTime = max;
			n.delayTime = new FakeParam(0);
			return n;
		},
		createBiquadFilter() {
			const n = ctx._node('biquad');
			n.type = 'lowpass';
			n.frequency = new FakeParam(350);
			n.Q = new FakeParam(1);
			n.gain = new FakeParam(0);
			return n;
		},
		createWaveShaper() {
			const n = ctx._node('waveshaper');
			n.curve = null;
			n.oversample = 'none';
			return n;
		},
		createConvolver() {
			const n = ctx._node('convolver');
			n.buffer = null;
			return n;
		},
		createBuffer(channels, length, sampleRate) {
			return { length, sampleRate, numberOfChannels: channels, copyToChannel() {} };
		}
	};
	Object.defineProperty(ctx, 'created', { value: [], enumerable: false });
	return ctx;
}

class FakeWorkletNode extends FakeNode {
	constructor(ctx, name, options) {
		super('worklet', ctx);
		this.name = name;
		this.options = options;
		this.parameters = new Map([
			['travel', new FakeParam(0)],
			['parameter', new FakeParam(0.5)]
		]);
		ctx.created.push(this);
	}
}

function channelState() {
	return {
		trim: 0.5,
		eq_low: 0.5,
		eq_mid: 0.5,
		eq_high: 0.5,
		filter: 0.5,
		fader: 1,
		cue_enabled: false,
		assign: 'thru'
	};
}

function buildChannels(ctx) {
	const byDeck = {};
	const channels = { 1: channelState(), 2: channelState(), 3: channelState(), 4: channelState() };
	channel.buildDeckChannelGraph({
		ctx,
		mixerState: { channels, crossfader: 0.5 },
		masterGain: ctx.createGain(),
		externalMerger: null,
		routing: null,
		cueSum: ctx.createGain(),
		xfGainFor: () => 1,
		onDeck: (deck, nodes) => {
			byDeck[deck] = nodes;
		}
	});
	return byDeck;
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

describe('live channel', () => {
	let hadNode;
	beforeEach(() => {
		live._resetColorFxForTests();
		hadNode = 'AudioWorkletNode' in globalThis;
		globalThis.AudioWorkletNode = FakeWorkletNode;
	});
	after(() => {
		if (!hadNode) delete globalThis.AudioWorkletNode;
	});

	test('the module loads once per context, only when a channel first selects CRUSH', async () => {
		const urls = [];
		const ctx = fakeContext(async (url) => {
			urls.push(url);
		});
		const decks = buildChannels(ctx);
		for (const effect of ['filter', 'space', 'echo']) {
			live.applyColorFxSettings({ deck: 1, effect });
			live.applyColorKnob(1, decks[1], 0.2);
		}
		assert.equal(urls.length, 0, 'nothing loads off CRUSH');
		live.applyColorFxSettings({ deck: 1, effect: 'crush' });
		live.applyColorKnob(1, decks[1], 0.2);
		assert.equal(live.colorFxView()[1].inert_reason, live.CRUSH_LOADING_REASON);
		assert.equal(decks[1].filterDry.gain.landing(), 1, 'dry while loading');
		live.applyColorFxSettings({ deck: 2, effect: 'crush' });
		live.applyColorKnob(2, decks[2], 0.2);
		await settle();
		assert.equal(urls.length, 1, 'one load for two channels');
		assert.match(urls[0], /color-fx-crush-processor\.js$/);
		assert.ok(
			live._crushGraphForTests(1) !== null && live._crushGraphForTests(2) !== null,
			'both spliced once ready'
		);
		assert.equal(live.colorFxView()[1].inert_reason, null);
		const g = live._crushGraphForTests(1);
		assert.equal(g.node.name, 'mdt-color-fx-crush');
		assert.equal(g.node.options.channelInterpretation, 'discrete');
		assert.ok(decks[1].high.outputs.has(g.node) && g.node.outputs.has(g.highpass));
		assert.ok(
			g.highpass.outputs.has(g.trim) &&
				g.trim.outputs.has(g.wet) &&
				g.wet.outputs.has(decks[1].colorBus)
		);
		live.applyColorFxSettings({ deck: 1, effect: 'filter' });
		live.applyColorFxSettings({ deck: 1, effect: 'crush' });
		await settle();
		assert.equal(urls.length, 1, 'reselecting never reloads');
	});

	test('a failed load names CRUSH_UNAVAILABLE and the channel stays dry', async () => {
		const ctx = fakeContext(async () => {
			throw new Error('404 processor');
		});
		const decks = buildChannels(ctx);
		const errors = [];
		const origError = console.error;
		console.error = (msg) => errors.push(msg);
		try {
			live.applyColorFxSettings({ deck: 3, effect: 'crush' });
			live.applyColorKnob(3, decks[3], 0.1);
			await settle();
		} finally {
			console.error = origError;
		}
		assert.match(live.colorFxView()[3].inert_reason, /^CRUSH_UNAVAILABLE: .*404 processor/);
		assert.equal(live._crushGraphForTests(3), null);
		assert.equal(decks[3].filterDry.gain.landing(), 1);
		assert.equal(errors.length, 1, 'logged once');
	});

	test('a terminal processorerror marks CRUSH_UNAVAILABLE, closes the wet and plays dry', async () => {
		const ctx = fakeContext(async () => {});
		const decks = buildChannels(ctx);
		live.applyColorFxSettings({ deck: 1, effect: 'crush' });
		live.applyColorKnob(1, decks[1], 0.15);
		await settle();
		live.applyColorKnob(1, decks[1], 0.15);
		const g = live._crushGraphForTests(1);
		assert.equal(
			typeof g.node.onprocessorerror,
			'function',
			'if no processorerror handler then broken'
		);
		assert.equal(decks[1].filterDry.gain.landing(), 0, 'crush owns dry at full CCW');
		const errors = [];
		const origError = console.error;
		console.error = (msg) => errors.push(String(msg));
		try {
			g.node.onprocessorerror({ type: 'processorerror', message: 'boom in process()' });
			g.node.onprocessorerror({ type: 'processorerror', message: 'again' });
		} finally {
			console.error = origError;
		}
		const reason = live.colorFxView()[1].inert_reason;
		assert.match(reason, /^CRUSH_UNAVAILABLE: .*processor error: boom in process\(\)/);
		assert.equal(g.failure, 'boom in process()');
		assert.equal(g.wet.gain.landing(), 0, 'if the failed wet stays open then broken');
		assert.equal(
			decks[1].filterDry.gain.landing(),
			1,
			'if the channel does not play dry then broken'
		);
		assert.equal(
			errors.filter((e) => e.startsWith('[color-fx]')).length,
			1,
			'logged once (sticky)'
		);
		const worklets = ctx.created.filter((n) => n instanceof FakeWorkletNode).length;
		live.applyColorFxSettings({ deck: 1, effect: 'filter' });
		live.applyColorFxSettings({ deck: 1, effect: 'crush' });
		live.applyColorKnob(1, decks[1], 0.15);
		await settle();
		assert.equal(
			live._crushGraphForTests(1),
			g,
			'if reselecting rebuilds the failed node then broken'
		);
		assert.equal(ctx.created.filter((n) => n instanceof FakeWorkletNode).length, worklets);
		assert.equal(live.colorFxView()[1].inert_reason, reason);
		assert.equal(g.wet.gain.landing(), 0);
		assert.equal(decks[1].filterDry.gain.landing(), 1);
	});

	test('the knob crossfades dry and wet linearly over the crossfade time', async () => {
		const ctx = fakeContext(async () => {});
		const decks = buildChannels(ctx);
		live.applyColorFxSettings({ deck: 1, effect: 'crush' });
		live.applyColorKnob(1, decks[1], 0.5);
		await settle();
		const g = live._crushGraphForTests(1);
		const dry = decks[1].filterDry.gain;
		live.applyColorKnob(1, decks[1], 0.15);
		const end = ctx.currentTime + crush.CRUSH_WET_CROSSFADE_SEC;
		assert.deepEqual(dry.events.at(-1), ['linear', 0, end]);
		assert.deepEqual(g.wet.gain.events.at(-1), ['linear', 1, end]);
		assert.ok(g.node.parameters.get('travel').landing() < 0, 'CCW travel');
		assert.equal(g.node.parameters.get('parameter').landing(), crush.CRUSH_PARAMETER_NOMINAL);
		assert.equal(g.highpass.frequency.landing(), 20);
		live.applyColorKnob(1, decks[1], 0.5);
		assert.deepEqual(dry.events.at(-1), ['linear', 1, end]);
		assert.deepEqual(g.wet.gain.events.at(-1), ['linear', 0, end]);
		live.applyColorKnob(1, decks[1], 1);
		assert.ok(Math.abs(g.highpass.frequency.landing() - 8000) < 1e-9, 'CW sweeps the highpass');
		// Leaving CRUSH closes the wet path and hands the dry gain back to FILTER.
		live.applyColorFxSettings({ deck: 1, effect: 'filter' });
		assert.deepEqual(g.wet.gain.events.at(-1), ['linear', 0, end]);
		assert.equal(dry.events.at(-1)[0], 'target');
	});
});

//--------------------------------------------------------------------- agent parity

describe('agent parity', () => {
	test('the dispatcher accepts crush and the mirror reports it; Rust mode refuses it by name', async () => {
		live._resetColorFxForTests();
		assert.deepEqual(
			ipc.parsePerformanceCommandForTest({ type: 'color_fx', deck: 1, effect: 'crush' }),
			{
				type: 'color_fx',
				deck: 1,
				effect: 'crush'
			}
		);
		assert.equal(pure.COLOR_FX_LABELS.crush, 'CRUSH');
		const hadWindow = 'window' in globalThis;
		if (!hadWindow) globalThis.window = {};
		const hadLocation = 'location' in globalThis;
		if (!hadLocation) globalThis.location = new URL('http://127.0.0.1/performance');
		const uninstall = ipc.installPerformanceBrowserIpc();
		try {
			await globalThis.window.musicDjToolsPerformance.dispatch({
				type: 'color_fx',
				deck: 1,
				effect: 'crush'
			});
			assert.equal(ipc.queryPerformanceState().color_fx[1].effect, 'crush');
		} finally {
			uninstall();
			if (!hadWindow) delete globalThis.window;
			if (!hadLocation) delete globalThis.location;
		}
		rust.rustMode.enabled = true;
		try {
			await assert.rejects(
				rust.executeInRustEngine({ type: 'filter', deck: 1, value: 0.2 }),
				/RUST_ENGINE_UNAVAILABLE: .*deck 1.*CRUSH/
			);
		} finally {
			rust.rustMode.enabled = false;
			live._resetColorFxForTests();
		}
	});
});
