// requirement: BEATFX-14398
// Beat FX FILTER and ROLL as unit effects (issue #9794): the real channel graph, unit and slot
// on a recording context; the return is checked by a linear evaluation of the built graph.
import assert from 'node:assert/strict';
import { before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let channel, insert, live, fx, buildFlags, panel;

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
function fakeContext() {
	const ctx = {
		currentTime: 10,
		sampleRate: 48000,
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
		createChannelMerger: () => ctx._node('merger', {}),
		createPeriodicWave: (real, imag) => ({ real: [...real], imag: [...imag] }),
		createOscillator: () =>
			Object.assign(ctx._node('osc', { frequency: 440 }), {
				setPeriodicWave(w) {
					this.wave = w;
				},
				start(t) {
					this.startedAt = t;
				},
				stop(t = 0) {
					this.stoppedAt = t;
				}
			})
	};
	Object.defineProperty(ctx, 'created', { value: [], enumerable: false });
	return ctx;
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
	const ctx = fakeContext();
	const masterGain = ctx.createGain();
	channel.buildDeckChannelGraph({
		ctx,
		mixerState: { channels: { 1: STRIP, 2: STRIP, 3: STRIP, 4: STRIP }, crossfader: 0.5 },
		masterGain,
		externalMerger: null,
		routing: null,
		cueSum: ctx.createGain(),
		xfGainFor: () => 1,
		onDeck: () => undefined
	});
	return { ctx, unit: insert.liveBeatFxInsert() };
}

/** The settled linear map of `from` to `to` as { x, lp } coefficients (x = send, lp = lowpass of x). */
function transfer(from, to) {
	const sum = { x: 0, lp: 0 };
	const walk = (node, gain, filtered) => {
		const g = node.kind === 'gain' ? gain * node.gain.settled() : gain;
		if (node === to) sum[filtered ? 'lp' : 'x'] += g;
		const lp = filtered || (node.kind === 'biquad' && node.type === 'lowpass');
		for (const next of node.outputs ?? []) walk(next, g, lp);
	};
	walk(from, 1, false);
	return sum;
}
const oscillators = (ctx) => ctx.created.filter((n) => n.kind === 'osc');

before(async () => {
	({ channel, insert, live, fx, buildFlags } = await loadTypeScriptModule(
		'tests/unit/fixtures/beat-fx-unit-entry.ts',
		{ viteApiBase: 'http://127.0.0.1:9' }
	));
	panel = await loadTypeScriptModule('src/lib/rb/beat-fx-panel.ts');
});
beforeEach(() => {
	live.beatFxUnit.tempo_bpm = 128;
	live.applyBeatFx({ enabled: false, level: 0.5, target: 'master', effect: 'echo', beats: 1 });
});

test('beat_fx effect filter puts the LFO filter kernel in the unit slot', () => {
	// [if] beat_fx {effect: filter} on does not build one lowpass and one LFO in the slot [then] FILTER plays nothing, [else stop].
	const { ctx, unit } = buildChannels();
	live.applyBeatFx({ effect: 'filter', level: 0.6, enabled: true });
	assert.equal(unit.slot.effect, 'filter');
	assert.equal(live.beatFxUnit.effect, 'filter');
	const lps = ctx.created.filter((n) => n.kind === 'biquad' && n.type === 'lowpass');
	assert.ok(lps.some((lp) => transfer(unit.unitIn, lp).x === 1));
	assert.equal(oscillators(ctx).length, 1);
});

test('the beat buttons set the LFO period and a level move keeps the same LFO', () => {
	// [if] beats 2 at 128 BPM is not a 0.9375 s LFO, or a level move restarts it [then] the beat buttons miss FILTER, [else stop].
	const { ctx } = buildChannels();
	live.applyBeatFx({ effect: 'filter', beats: 2, level: 0.6, enabled: true });
	assert.ok(Math.abs(1 / oscillators(ctx)[0].frequency.value - (2 * 60) / 128) < 1e-9);
	live.applyBeatFx({ level: 0.3 });
	assert.equal(oscillators(ctx).length, 1);
	ctx.currentTime = 10.5;
	live.applyBeatFx({ beats: 1 });
	const [old, next] = oscillators(ctx);
	assert.equal(old.stoppedAt, 10.5);
	assert.ok(Math.abs(1 / next.frequency.value - 60 / 128) < 1e-9);
	// Phase kept: 0.5 s into a 0.9375 s cycle is phase 0.5333, so the new wave starts there.
	const phase = 0.5 / 0.9375;
	assert.ok(Math.abs(next.wave.real[1] + Math.cos(2 * Math.PI * phase)) < 1e-6);
});

test('dry plus the slot return is the filter balance, and level 0 returns exactly nothing', () => {
	// [if] dry + return is not (1 - L) x + L LP(x), or level 0 returns anything but exact 0 [then] FILTER is not an insert, [else stop].
	const { unit } = buildChannels();
	live.applyBeatFx({ effect: 'filter', level: 0.7, enabled: true });
	const ret = transfer(unit.slot.kernel.input, unit.slot.kernel.output);
	// The level curve is float32, so 0.7 lands within 1e-7.
	assert.ok(Math.abs(1 + ret.x - 0.3) < 1e-6 && Math.abs(ret.lp - 0.7) < 1e-6);
	live.applyBeatFx({ level: 0 });
	assert.deepEqual(transfer(unit.slot.kernel.input, unit.slot.kernel.output), { x: 0, lp: 0 });
});

test('FILTER off closes every send and leaves dry exact; another effect disposes the LFO', () => {
	// [if] OFF leaves a send open or a dry off 1, or a swap leaves the LFO running [then] FILTER leaks after release, [else stop].
	const { ctx, unit } = buildChannels();
	live.applyBeatFx({ effect: 'filter', level: 0.7, enabled: true });
	live.applyBeatFx({ enabled: false });
	for (const send of unit.sends.values()) assert.equal(send.gain.settled(), 0);
	for (const dry of unit.drys.values()) assert.equal(dry.gain.settled(), 1);
	live.applyBeatFx({ effect: 'echo', enabled: true });
	assert.equal(unit.slot.effect, 'echo');
	assert.notEqual(oscillators(ctx)[0].stoppedAt, undefined);
});

test('the panel lists FILTER as built and ROLL as unbuilt; ROLL and unknown names are refused', () => {
	// [if] the panel lacks filter or roll, or roll or an unknown name changes the unit [then] the effect list lies, [else stop].
	const options = panel.beatFxEffectOptions();
	assert.deepEqual(
		options.filter((o) => o.name === 'filter' || o.name === 'roll'),
		[
			{ name: 'filter', built: true },
			{ name: 'roll', built: false }
		]
	);
	buildChannels();
	assert.throws(() => live.applyBeatFx({ effect: 'roll', enabled: true }), /roll.*not built/);
	assert.throws(() => live.applyBeatFx({ effect: 'lfo_filter' }), /lfo_filter/);
	assert.equal(live.beatFxUnit.effect, 'echo');
	assert.equal(fx.beatFxEffectDef('roll').createKernel, undefined);
});

test('FILTER refuses a division outside its range by name', () => {
	// [if] the filter def accepts 3/8 beats or a 33 s cycle, or the error omits the value [then] FILTER bounds leak, [else stop].
	const def = fx.beatFxEffectDef('filter');
	assert.throws(() => def.assertDivisionAllowed(3 / 8, 128), /0\.375/);
	assert.throws(() => def.assertDivisionAllowed(16, 29), /timeMs/);
	def.assertDivisionAllowed(1 / 16, 300);
});

test('Rust engine mode refuses beat_fx filter and roll naming the effect', async () => {
	// [if] Rust mode lets beat_fx filter or roll through, or the refusal omits the effect name [then] it plays dry silently, [else stop].
	const rust = await loadTypeScriptModule('src/lib/audio-engine/rust-mode.svelte.ts');
	rust.rustMode.enabled = true;
	for (const effect of ['filter', 'roll']) {
		await assert.rejects(
			rust.executeInRustEngine({ type: 'beat_fx', effect, enabled: true }),
			new RegExp(`^Error: RUST_ENGINE_UNAVAILABLE: beat_fx effect ${effect} `)
		);
	}
});
