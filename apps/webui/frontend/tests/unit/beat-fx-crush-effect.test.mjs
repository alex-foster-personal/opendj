// requirement: BEATFX-14526
// Beat FX BIT CRUSH as a unit effect (issue #10018): the real channel graph, unit and slot on a
// recording context; the hosted return is rendered with the real CRUSH processor's process().
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, beforeEach, test } from 'node:test';
import vm from 'node:vm';

import { loadTypeScriptModule } from './load-typescript.mjs';

const PROCESSOR_PATH = new URL('../../src/lib/rb/color-fx-crush-processor.js', import.meta.url);
const SR = 48000;
const BLOCK = 128;
const FLT_MIN = 1.1754943508222875e-38;

let channel, insert, live, fx, buildFlags, crush, panel;

class FakeParam {
	constructor(value) {
		this.value = value;
		this.events = [];
	}
	cancelScheduledValues(t) {
		this.events = this.events.filter((e) => e[2] < t);
	}
	cancelAndHoldAtTime(t) {
		this.events = this.events.filter((e) => e[2] < t);
	}
	setValueAtTime(v, t) {
		this.events.push(['set', v, t]);
	}
	linearRampToValueAtTime(v, t) {
		this.events.push(['linear', v, t]);
	}
	setTargetAtTime(v, t) {
		this.events.push(['target', v, t]);
	}
	setValueCurveAtTime(curve, t, d) {
		this.events.push(['curve', curve[curve.length - 1], t, d]);
	}
	/** The value the newest event leaves, or the static value. */
	settled() {
		return this.events.length === 0 ? this.value : this.events[this.events.length - 1][1];
	}
}
// Fake nodes keep their context non-enumerable so a failing assert never inspects the whole graph.
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
function fakeContext() {
	const urls = [];
	let release;
	const loaded = new Promise((resolve) => (release = resolve));
	const ctx = {
		currentTime: 10,
		sampleRate: SR,
		audioWorklet: { addModule: (url) => (urls.push(url), loaded) },
		_node(kind, params) {
			const n = new FakeNode(kind, ctx);
			for (const [name, value] of Object.entries(params)) n[name] = new FakeParam(value);
			ctx.created.push(n);
			return n;
		},
		createGain: () => ctx._node('gain', { gain: 1 }),
		createAnalyser: () => ctx._node('analyser', {}),
		createDelay: (max) =>
			Object.assign(ctx._node('delay', { delayTime: 0 }), { maxDelayTime: max }),
		createBiquadFilter: () => ctx._node('biquad', { frequency: 350, Q: 1, gain: 0, detune: 0 }),
		createWaveShaper: () => Object.assign(ctx._node('waveshaper', {}), { curve: null }),
		createChannelSplitter: () => ctx._node('splitter', {}),
		createChannelMerger: () => ctx._node('merger', {})
	};
	Object.defineProperty(ctx, 'created', { value: [], enumerable: false });
	return { ctx, urls, release };
}
const STRIP = {
	trim: 0.5,
	eq_low: 0.5,
	eq_mid: 0.5,
	eq_high: 0.5,
	filter: 0.5,
	fader: 1,
	cue_enabled: true,
	assign: 'thru'
};

/** Build the real four-deck channel graph with `fx.beat_rack` on. */
function buildChannels() {
	buildFlags.flags = [{ flag_id: 'fx.beat_rack', enabled: true }];
	const fake = fakeContext();
	const masterGain = fake.ctx.createGain();
	channel.buildDeckChannelGraph({
		ctx: fake.ctx,
		mixerState: { channels: { 1: STRIP, 2: STRIP, 3: STRIP, 4: STRIP }, crossfader: 0.5 },
		masterGain,
		externalMerger: null,
		routing: null,
		cueSum: fake.ctx.createGain(),
		xfGainFor: () => 1,
		onDeck: () => undefined
	});
	return { ...fake, unit: insert.liveBeatFxInsert() };
}
const settle = () => new Promise((resolve) => setImmediate(resolve));
const worklets = (ctx) => ctx.created.filter((n) => n.kind === 'worklet');

/** Engage crush at `level`, let the worklet load, and return the slot's nodes. */
async function engageCrush(level) {
	const built = buildChannels();
	live.applyBeatFx({ effect: 'crush', level, enabled: true });
	built.release();
	await settle();
	const [node] = worklets(built.ctx);
	const [wet, cancel] = [...built.unit.slot.kernel.input.outputs].map((n) =>
		n === node ? [...node.outputs][0] : n
	);
	return { ...built, node, wet, cancel };
}

function loadProcessorClass() {
	const registered = {};
	const sandbox = {
		AudioWorkletProcessor: class {},
		registerProcessor: (name, cls) => (registered[name] = cls),
		sampleRate: SR,
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
/** Run a processor built from `options` over whole quanta at constant travel and parameter. */
function runProcessor(options, input, travel, parameter) {
	const proc = new (loadProcessorClass())({ processorOptions: options });
	const out = [new Float32Array(input[0].length), new Float32Array(input[0].length)];
	for (let s = 0; s < input[0].length; s += BLOCK) {
		const e = s + BLOCK;
		const outputs = [[out[0].subarray(s, e), out[1].subarray(s, e)]];
		const params = { travel: new Float32Array([travel]), parameter: new Float32Array([parameter]) };
		proc.process([input.map((c) => c.subarray(s, e))], outputs, params);
	}
	return { out, proc };
}
/** dry + slot return as the graph sums them: x + wet * crush(x) + cancel * x, per channel. */
function hostedOutput(slot, input) {
	const travel = slot.node.parameters.get('travel').settled();
	const parameter = slot.node.parameters.get('parameter').settled();
	const { out } = runProcessor(slot.node.options.processorOptions, input, travel, parameter);
	const [w, k] = [slot.wet.gain.settled(), slot.cancel.gain.settled()];
	return input.map((x, c) => x.map((v, i) => v + (w * out[c][i] + k * v)));
}
function fixture(frames, peak = 10 ** (-3 / 20)) {
	let state = 0x10018;
	const noise = () => {
		state ^= state << 13;
		state ^= state >>> 17;
		state ^= state << 5;
		return ((state >>> 0) / 0xffffffff) * 2 - 1;
	};
	return [0, 1].map(() =>
		Float32Array.from({ length: frames }, (_, i) => {
			return peak * (0.5 * Math.sin((2 * Math.PI * 437 * i) / SR) + 0.5 * noise());
		})
	);
}

let hadNode;
before(async () => {
	({ channel, insert, live, fx, buildFlags, crush } = await loadTypeScriptModule(
		'tests/unit/fixtures/beat-fx-unit-entry.ts',
		{ viteApiBase: 'http://127.0.0.1:9' }
	));
	panel = await loadTypeScriptModule('src/lib/rb/beat-fx-panel.ts');
	hadNode = 'AudioWorkletNode' in globalThis;
	globalThis.AudioWorkletNode = FakeWorkletNode;
});
after(() => {
	if (!hadNode) delete globalThis.AudioWorkletNode;
});
beforeEach(() => {
	live.beatFxUnit.tempo_bpm = 128;
	live.applyBeatFx({ enabled: false, level: 0.5, target: 'master', effect: 'echo', beats: 1 });
});

test('crush is a unit effect: depth 0 is bit-exact dry, depth follows the taper, wet reaches 1 at full', async () => {
	// [if] depth 0 is not bit-exact dry, or depth 1 is not 4 bits x64 hold at wet 1 [then] BIT CRUSH is not the knob's crush, [else stop].
	const slot = await engageCrush(0.6);
	assert.equal(slot.unit.slot.effect, 'crush');
	assert.equal(slot.node.parameters.get('travel').settled(), -0.6);
	assert.deepEqual([slot.wet.gain.settled(), slot.cancel.gain.settled()], [0.6, -0.6]);
	const input = fixture(SR / 4);
	live.applyBeatFx({ level: 0 });
	const dry = hostedOutput(slot, input);
	for (let c = 0; c < 2; c++)
		for (let i = 0; i < input[c].length; i++) assert.equal(dry[c][i], input[c][i]);
	live.applyBeatFx({ level: 1 });
	assert.deepEqual([slot.wet.gain.settled(), slot.cancel.gain.settled()], [1, -1]);
	const { proc } = runProcessor(slot.node.options.processorOptions, input, -1, 0.5);
	assert.ok(Math.abs(proc.bits - 4) < 1e-6 && Math.abs(proc.decimation - 64) < 1e-4);
	assert.equal(crush.crushDecimationFor(0.5), 8);
});

test('crush ignores the beat division: no beats needed, the mirror says null, the readout is empty', () => {
	// [if] crush needs beats, the mirror shows a division or ms, or the panel prints 0 ms [then] the BEAT readout lies, [else stop].
	buildChannels();
	live.applyBeatFx({ effect: 'crush', level: 0.4, enabled: true });
	const view = live.beatFxView();
	assert.deepEqual([view.effect, view.beats, view.beat_ms], ['crush', null, null]);
	assert.equal(panel.beatFxTimeReadout(view), '');
	assert.equal(panel.beatFxDivisionLabel(view.beats), '');
	assert.equal(panel.beatFxBeatStepCommand(view.beats, 1), null);
	live.applyBeatFx({ effect: 'echo' });
	assert.deepEqual([live.beatFxView().beats, live.beatFxView().beat_ms], [1, 468.75]);
});

test('the crush module loads once per context, shared with the color knob, and matches it sample for sample', async () => {
	// [if] a reselect or the color knob loads the module again, or hosted != knob crush at equal depth [then] two kernels drift, [else stop].
	const slot = await engageCrush(0.7);
	live.applyBeatFx({ effect: 'echo' });
	live.applyBeatFx({ effect: 'crush' });
	await settle();
	const shared = crush.loadCrushWorklet(slot.ctx, 'color-knob-url');
	assert.equal(shared, crush.crushLoadState(slot.ctx));
	assert.deepEqual([slot.urls.length, shared.state], [1, 'ready']);
	assert.deepEqual(slot.node.options.processorOptions, crush.crushProcessorOptions());
	const input = fixture(SR / 4);
	const hosted = runProcessor(slot.node.options.processorOptions, input, -0.7, 0.5).out;
	const knob = crush.renderCrush(
		input,
		new Float32Array([-0.7]),
		crush.CRUSH_PARAMETER_NOMINAL,
		SR
	);
	for (let c = 0; c < 2; c++) assert.deepEqual(hosted[c], knob[c]);
});

test('offline render: no sample over 0 dBFS, no NaN or subnormal, at most 128 samples of latency', async () => {
	// [if] any depth 0..1 on a -3 dBFS fixture peaks over 1, goes non-finite or subnormal, or lags over 128 [then] crush is unsafe, [else stop].
	const slot = await engageCrush(0.1);
	const input = fixture(SR / 2);
	for (let step = 0; step <= 10; step++) {
		live.applyBeatFx({ level: step / 10 });
		for (const ch of hostedOutput(slot, input)) {
			for (const v of ch) {
				assert.ok(Number.isFinite(v) && Math.abs(v) <= 1, `depth ${step / 10}: sample ${v}`);
				assert.ok(v === 0 || Math.abs(v) >= FLT_MIN, `depth ${step / 10}: subnormal ${v}`);
			}
		}
	}
	// Latency: a step at frame 2000 reaches the crusher's output within one render quantum at every depth.
	const stepAt = 2000;
	const step = [0, 1].map(() =>
		Float32Array.from({ length: BLOCK * 24 }, (_, i) => (i < stepAt ? 0 : 0.5))
	);
	for (const depth of [0, 0.25, 0.5, 0.75, 1]) {
		const { out } = runProcessor(slot.node.options.processorOptions, step, -depth, 0.5);
		const first = out[0].findIndex((v) => v !== 0);
		assert.ok(first >= stepAt && first - stepAt <= BLOCK, `depth ${depth}: step lands at ${first}`);
	}
});

test('Rust engine mode refuses beat_fx crush naming the effect; the typed parser accepts it', async () => {
	// [if] Rust mode lets beat_fx crush through or omits its name, or the parser refuses crush [then] agents lose parity, [else stop].
	const beatFx = await loadTypeScriptModule('src/lib/rb/beat-fx.ts');
	assert.deepEqual(beatFx.parseBeatFxCommand({ type: 'beat_fx', effect: 'crush', level: 0.3 }), {
		type: 'beat_fx',
		effect: 'crush',
		level: 0.3
	});
	assert.equal(fx.beatFxEffectDef('crush').builtOnMain, true);
	const rust = await loadTypeScriptModule('src/lib/audio-engine/rust-mode.svelte.ts');
	rust.rustMode.enabled = true;
	await assert.rejects(
		rust.executeInRustEngine({ type: 'beat_fx', effect: 'crush', enabled: true }),
		/^Error: RUST_ENGINE_UNAVAILABLE: beat_fx effect crush /
	);
});
