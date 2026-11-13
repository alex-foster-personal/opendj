// requirement: FXVERB-14576
// Beat FX REVERB (issue #9821): the pure parameter law, and the real channel graph, unit and slot
// on a recording context; tails are rendered offline from the slot's own impulse.
import assert from 'node:assert/strict';
import { before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SR = 48000;
const NOW = 10;
let law, unit, blend;

class FakeParam {
	constructor(value) {
		this.value = value;
		this.events = [];
	}
	cancelScheduledValues(t) {
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
	settled() {
		return this.events.length === 0 ? this.value : this.events.at(-1)[1];
	}
	/** The scheduled value at context time `t` (set and linear events). */
	at(t) {
		let [value, from] = [this.value, -Infinity];
		for (const [kind, v, et] of this.events) {
			if (t < et)
				return kind === 'linear' ? value + ((v - value) * (t - from)) / (et - from) : value;
			[value, from] = [v, et];
		}
		return value;
	}
}
class FakeNode {
	constructor(kind) {
		this.kind = kind;
		this.outputs = new Set();
	}
	connect(node) {
		this.outputs.add(node);
		return node;
	}
	disconnect() {
		this.outputs.clear();
	}
}
function fakeContext() {
	const created = [];
	const node = (kind, params, extra = {}) => {
		const n = Object.assign(new FakeNode(kind), extra);
		for (const [name, value] of Object.entries(params)) n[name] = new FakeParam(value);
		created.push(n);
		return n;
	};
	const ctx = {
		currentTime: NOW,
		sampleRate: SR,
		createGain: () => node('gain', { gain: 1 }),
		createAnalyser: () => node('analyser', {}),
		createDelay: (max) => node('delay', { delayTime: 0 }, { maxDelayTime: max }),
		createBiquadFilter: () => node('biquad', { frequency: 350, Q: 1, gain: 0, detune: 0 }),
		createWaveShaper: () => node('waveshaper', {}, { curve: null }),
		createChannelSplitter: () => node('splitter', {}),
		createChannelMerger: () => node('merger', {}),
		createConvolver: () => node('convolver', {}, { buffer: null, normalize: false }),
		createBuffer: (channels, length) => ({
			data: Array.from({ length: channels }, () => new Float32Array(length)),
			copyToChannel(src, c) {
				this.data[c].set(src);
			}
		})
	};
	Object.defineProperty(ctx, 'created', { value: created, enumerable: false });
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

/** Build the real four-deck channel graph with `fx.beat_rack` on and engage REVERB at `level`. */
function engageReverb(level) {
	unit.buildFlags.flags = [{ flag_id: 'fx.beat_rack', enabled: true }];
	const ctx = fakeContext();
	const masterGain = ctx.createGain();
	unit.channel.buildDeckChannelGraph({
		ctx,
		mixerState: { channels: { 1: STRIP, 2: STRIP, 3: STRIP, 4: STRIP }, crossfader: 0.5 },
		masterGain,
		externalMerger: null,
		routing: null,
		cueSum: ctx.createGain(),
		xfGainFor: () => 1,
		onDeck: () => undefined
	});
	unit.live.applyBeatFx({ effect: 'reverb', level, enabled: true });
	const insert = unit.insert.liveBeatFxInsert();
	const kernel = insert.slot.kernel;
	const wet = ctx.created.find((n) => n.outputs.has(kernel.output));
	const voices = () => ctx.created.filter((n) => n.kind === 'convolver');
	return { ctx, insert, kernel, wet, voices };
}
/** -3 dBFS 437 Hz sine plus seeded noise: the shared fixture shape. */
function fixture(frames) {
	let state = 0x9821;
	return Float32Array.from({ length: frames }, (_, i) => {
		state ^= state << 13;
		state ^= state >>> 17;
		state ^= state << 5;
		const noise = ((state >>> 0) / 0xffffffff) * 2 - 1;
		return 0.708 * (0.5 * Math.sin((2 * Math.PI * 437 * i) / SR) + 0.5 * noise);
	});
}
/** Direct convolution of `x * gains` with `h`, for frames [lo, hi) only. */
function convolveWindow(x, h, lo, hi, gains = null) {
	const out = new Float32Array(x.length);
	for (let n = lo; n < hi; n++) {
		let acc = 0;
		for (let k = 0; k < h.length && k <= n; k++) acc += h[k] * x[n - k] * (gains?.[n - k] ?? 1);
		out[n] = acc;
	}
	return out;
}
const EVENT = Math.round(SR * 0.6);
/** Context time of `frame`; events are scheduled at NOW, and before it a param held its NOW value. */
const timeOf = (frame) => NOW + Math.max(0, frame - EVENT) / SR;

before(async () => {
	law = await loadTypeScriptModule('src/lib/rb/beat-fx-reverb.ts');
	blend = await loadTypeScriptModule('src/lib/rb/slip-return-blend.ts');
	unit = await loadTypeScriptModule('tests/unit/fixtures/beat-fx-unit-entry.ts', {
		viteApiBase: 'http://127.0.0.1:9'
	});
});
beforeEach(() => {
	unit.live.beatFxUnit.release_held = false;
	unit.live.applyBeatFx({ enabled: false, level: 0.5, target: 'master', effect: 'echo', beats: 1 });
});

test('amount steps, TIME and LEVEL/DEPTH map to the kernel params; any other amount is refused by name', () => {
	// [if] amount 1/50/100 is not 0.4/2.6/8 s log-spaced, TIME not 0.5..1.5x pre-delay, or 33 passes [then] the DJM surface is wrong, [else stop].
	const rt = law.REVERB_AMOUNT_STEPS.map((a) => law.reverbAmountToRt60(a));
	assert.deepEqual(
		[rt[0], rt[3], rt[6]].map((s) => +s.toFixed(12)),
		[0.4, 2.6, 8]
	);
	for (let i = 1; i < rt.length; i++) assert.ok(rt[i] > rt[i - 1], `RT60 rises at step ${i}`);
	assert.ok(Math.abs(rt[2] - 0.4 * 6.5 ** (24 / 49)) < 1e-12, 'amount 25 sits on the log law');
	assert.deepEqual([1, 50.5, 100].map(law.reverbTimeToSize), [0.5, 1, 1.5]);
	assert.deepEqual([0, 0.5, 1].map(law.reverbWetForLevel), [0, 0.4, 0.8]);
	for (const bad of [33, 0, '50', 50.5, NaN])
		assert.throws(
			() => law.reverbAmountToRt60(bad),
			/reverb amount must be one of 1\|10\|25\|50\|75\|90\|100; got/
		);
	assert.throws(() => law.reverbTimeToSize(0), /reverb time must be a number in 1\.\.100/);
	assert.throws(
		() => law.assertReverbRange('filter_hz', 100),
		/reverb filter_hz must be a number in 500\.\.20000/
	);
	// The kernel: amount 1 at TIME 100 is a 0.4 s impulse behind a 30 ms pre-delay, filter at 2 kHz.
	const slot = engageReverb(0.5);
	assert.equal(slot.voices()[0].buffer.data[0].length, Math.round(SR * 2.6));
	slot.kernel.setReverb({ amount: 1, time: 100, filter_hz: 2000 }, NOW);
	const voice = slot.voices()[1];
	const preDelay = slot.ctx.created.find((n) => n.outputs.has(voice));
	assert.deepEqual(
		[voice.normalize, voice.buffer.data[1].length, preDelay.delayTime.value],
		[true, SR * 0.4, 0.03]
	);
	assert.equal([...slot.wet.outputs][0], slot.kernel.output);
	assert.equal(slot.ctx.created.find((n) => n.outputs.has(slot.wet)).frequency.settled(), 2000);
	assert.throws(
		() => slot.kernel.setReverb({ amount: 33, time: 50, filter_hz: 2000 }, NOW),
		/amount must be one of/
	);
});

test('off rings the tail out and Release cuts it over 10 ms into a fresh voice, both at click ratio <= 1.5', () => {
	// [if] off cuts the tail, release does not fade every voice in 10 ms and restart clean, or a click ratio > 1.5 [then] tails misbehave, [else stop].
	const slot = engageReverb(1);
	slot.kernel.setReverb({ amount: 1, time: 50, filter_hz: 20000 }, NOW);
	const voice = slot.voices().at(-1);
	const h = voice.buffer.data[0];
	const x = fixture(EVENT + SR / 4);
	const [lo, hi] = [EVENT - 20, EVENT + 560];
	const dry = convolveWindow(x, h, lo, hi);
	unit.live.applyBeatFx({ enabled: false });
	assert.equal(slot.wet.gain.settled(), law.REVERB_WET_CEILING);
	const send = [...slot.insert.sends.values()][0].gain;
	assert.equal(send.settled(), 0);
	const gains = Float64Array.from(x, (_, f) => send.at(timeOf(f)));
	const off = convolveWindow(x, h, lo, hi, gains);
	assert.ok(blend.clickRatio(off, [dry], EVENT) <= blend.SLIP_RETURN_CLICK_VETO, 'off click ratio');
	const ringAt = EVENT + SR / 10;
	const ring = convolveWindow(x, h, ringAt, ringAt + 1, gains)[ringAt];
	assert.ok(Math.abs(ring) > 1e-3, `the tail still rings 100 ms after off: ${ring}`);
	unit.live.applyBeatFx({ enabled: true });
	const before = slot.voices().length;
	unit.live.applyBeatFxRelease({ held: true });
	const fade = [...voice.outputs][0].gain;
	assert.deepEqual(fade.events, [
		['set', 1, NOW],
		['linear', 0, NOW + 0.01]
	]);
	assert.equal(slot.voices().length, before + 1, 'a clean convolver takes the input');
	const cut = dry.map((v, f) => v * fade.at(timeOf(f)));
	assert.ok(
		blend.clickRatio(cut, [dry], EVENT) <= blend.SLIP_RETURN_CLICK_VETO,
		'release click ratio'
	);
	assert.ok(Math.abs(cut[hi - 1]) === 0, 'the cut return is silent after 10 ms');
});

test('LEVEL/DEPTH 0 returns exactly 0 from the slot, so dry plus return is bit-exact dry', () => {
	// [if] level 0 leaves any wet gain, or dry plus the slot return differs from dry by one bit [then] LEVEL/DEPTH CCW is not original sound, [else stop].
	const slot = engageReverb(0.7);
	unit.live.applyBeatFx({ level: 0 });
	assert.deepEqual(slot.wet.gain.events.at(-1), ['linear', 0, NOW + 0.01]);
	const x = fixture(SR / 10);
	const tail = fixture(SR / 10).reverse();
	const out = x.map((v, i) => v + slot.wet.gain.at(NOW + 0.01 + i / SR) * tail[i]);
	for (let i = 0; i < x.length; i++) assert.equal(out[i], x[i]);
});

test('Rust engine mode refuses beat_fx reverb by name with RUST_ENGINE_UNAVAILABLE', async () => {
	// [if] Rust mode lets beat_fx reverb through or omits its name [then] the native engine fakes a reverb it lacks, [else stop].
	const rust = await loadTypeScriptModule('src/lib/audio-engine/rust-mode.svelte.ts');
	rust.rustMode.enabled = true;
	await assert.rejects(
		rust.executeInRustEngine({ type: 'beat_fx', effect: 'reverb', enabled: true }),
		/^Error: RUST_ENGINE_UNAVAILABLE: beat_fx effect reverb /
	);
	assert.equal(unit.fx.beatFxEffectDef('reverb').builtOnMain, true);
});
