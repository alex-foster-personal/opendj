// requirement: FXECHO-14135
// MULTI TAP kernel (#10009) driven offline: the real multitap-kernel.ts builds its graph on a recording
// context, and a per-sample renderer walks that graph (gains a-rate, DelayNodes with linear interpolation).
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SR = 48000;
const BPM = 124;
const BEAT_SEC = 60 / BPM;
const FLT_MIN_NORMAL = 1.1754943508222875e-38;

/** set + linear automation (all the kernel schedules); `value` reads the context clock like Web Audio. */
class Param {
	constructor(clock, value) {
		this.clock = clock;
		this.base = value;
		this.events = [];
	}
	get value() {
		return this.valueAt(this.clock.t);
	}
	set value(v) {
		this.base = v;
	}
	setValueAtTime(v, t) {
		this._add({ kind: 'set', v, t });
	}
	linearRampToValueAtTime(v, t) {
		this._add({ kind: 'linear', v, t });
	}
	cancelScheduledValues(t) {
		this.events = this.events.filter((e) => e.t < t);
	}
	_add(e) {
		this.events.push(e);
		this.events.sort((a, b) => a.t - b.t);
	}
	valueAt(t) {
		let val = this.base;
		let lastT = 0;
		for (const e of this.events) {
			if (e.kind === 'set') {
				if (e.t > t) break;
				[val, lastT] = [e.v, e.t];
			} else if (e.t <= t) {
				[val, lastT] = [e.v, e.t];
			} else {
				return val + ((e.v - val) * (t - lastT)) / (e.t - lastT);
			}
		}
		return val;
	}
}

class FakeNode {
	constructor(kind) {
		this.kind = kind;
		this.outputs = [];
		this.sources = [];
		this.disconnected = false;
	}
	connect(node) {
		this.outputs.push(node);
		node.sources.push(this);
		return node;
	}
	disconnect() {
		this.disconnected = true;
	}
}

function fakeContext() {
	const ctx = {
		clock: { t: 0 },
		sampleRate: SR,
		created: [],
		createGain() {
			const n = new FakeNode('gain');
			n.gain = new Param(ctx.clock, 1);
			ctx.created.push(n);
			return n;
		},
		createDelay(max) {
			const n = new FakeNode('delay');
			n.maxDelayTime = max;
			n.delayTime = new Param(ctx.clock, 0);
			n.buf = new Float64Array(Math.ceil(max * SR) + 2);
			ctx.created.push(n);
			return n;
		}
	};
	return ctx;
}

/** Topological order of the (acyclic) kernel graph from its input. */
function topo(input) {
	const seen = new Set();
	const order = [];
	const visit = (n) => {
		if (seen.has(n)) return;
		seen.add(n);
		n.outputs.forEach(visit);
		order.push(n);
	};
	visit(input);
	return order.reverse();
}

/** Render `send` through kernel `k`; `events` are [atSec, (k) => void] calls made on the render clock. */
function render(ctx, k, send, seconds, events = []) {
	const order = topo(k.input);
	const out = new Float64Array(Math.round(seconds * SR));
	const y = new Map();
	const queue = [...events].sort((a, b) => a[0] - b[0]);
	for (let n = 0; n < out.length; n++) {
		const t = n / SR;
		while (queue.length && queue[0][0] <= t) {
			const [at, run] = queue.shift();
			ctx.clock.t = at;
			run(k);
		}
		ctx.clock.t = t;
		for (const node of order) {
			const x =
				node === k.input ? (send[n] ?? 0) : node.sources.reduce((s, src) => s + y.get(src), 0);
			if (node.kind === 'gain') {
				y.set(node, x * node.gain.valueAt(t));
				continue;
			}
			const len = node.buf.length;
			node.buf[n % len] = x;
			const d = node.delayTime.valueAt(t) * SR;
			const i0 = Math.floor(d);
			const f = d - i0;
			const read = (i) => (n - i < 0 ? 0 : node.buf[(n - i) % len]);
			y.set(node, (1 - f) * read(i0) + f * read(i0 + 1));
		}
		out[n] = y.get(k.output);
	}
	return out;
}

const impulse = (seconds, atSec = 0) => {
	const x = new Float64Array(Math.round(seconds * SR));
	x[Math.round(atSec * SR)] = 1;
	return x;
};

/** 66 Hz decaying kick, 4 cycles per 1/8 of a beat at 124 BPM: every tap of a 1-beat setting lands in phase. */
function inPhaseKick() {
	const f = 4 / (BEAT_SEC / 8);
	const x = new Float64Array(Math.round(0.4 * SR));
	for (let i = 0; i < x.length; i++)
		x[i] = Math.exp(-i / (0.08 * SR)) * Math.sin((2 * Math.PI * f * i) / SR);
	return x;
}

/** Clusters of samples above `floor`, split by gaps over 1 ms: one per audible tap. */
function onsets(out, floor = 0) {
	const found = [];
	let lastHit = -Infinity;
	for (let i = 0; i < out.length; i++) {
		if (Math.abs(out[i]) <= floor) continue;
		if (i - lastHit > 0.001 * SR) found.push({ at: i, sum: 0 });
		found[found.length - 1].sum += out[i];
		lastHit = i;
	}
	return found;
}

const peak = (a) => a.reduce((p, v) => Math.max(p, Math.abs(v)), 0);

describe('multitap-kernel', async () => {
	const m = await loadTypeScriptModule('src/lib/rb/fx/multitap-kernel.ts');
	const { buildMultiTapKernel, multitapTapGains, multitapSumGain } = m;
	const OPTS = { timeSec: BEAT_SEC, taps: 7, wet: 1, multitapLaw: 'decay' };
	const build = (opts = {}) => {
		const ctx = fakeContext();
		return { ctx, k: buildMultiTapKernel(ctx, { ...OPTS, ...opts }) };
	};

	test('[if] the surface, tap k at k * time / 8, or the taps and 10..4000 ms refusals are wrong [then] MULTI TAP misplaces taps, [else stop]', () => {
		const { k } = build();
		for (const name of ['setTime', 'gateInput', 'dispose', 'setTaps', 'setWet']) {
			assert.equal(typeof k[name], 'function', name);
		}
		assert.ok(k.input && k.output);
		for (const bank of k.delays) {
			bank.forEach((d, i) => assert.equal(d.delayTime.value, ((i + 1) * BEAT_SEC) / 8));
		}
		for (const bad of [0, 8, 2.5, NaN]) {
			assert.throws(
				() => k.setTaps(bad, 1),
				new RegExp(`multitap taps ${bad} is outside 1\\.\\.7`)
			);
		}
		for (const n of [1, 4, 7]) k.setTaps(n, 1);
		assert.throws(() => k.setTime(0.005, 1), /multitap time 5 ms is outside \[10, 4000\] ms/);
		assert.throws(() => k.setTime(4.5, 1), /multitap time 4500 ms is outside \[10, 4000\] ms/);
		assert.throws(() => build({ timeSec: 0.009 }), /multitap time 9 ms/);
		k.setTime(0.01, 1);
		k.setTime(4, 2);
		k.gateInput(false, 3);
		assert.equal(k.input.gain.valueAt(3 + m.MULTITAP_RAMP_SEC), 0);
		k.dispose();
		assert.ok(k.nodes.every((n) => n.disconnected));
		assert.throws(() => k.setTaps(3, 4), /disposed/);
	});

	test('[if] nodes are built after construction, setTaps moves delays, or a retune moves a live tap [then] taps glitch, [else stop]', () => {
		const { ctx, k } = build();
		assert.equal(ctx.created.filter((n) => n.kind === 'delay').length, 14);
		assert.equal(ctx.created.filter((n) => n.kind === 'gain').length, 18);
		const built = ctx.created.length;
		k.setTaps(3, 5);
		for (const bank of k.tapGains) {
			for (const tap of bank) {
				assert.deepEqual(
					tap.gain.events.map((e) => [e.kind, e.t]),
					[
						['set', 5],
						['linear', 5 + m.MULTITAP_RAMP_SEC]
					]
				);
			}
		}
		assert.ok(k.delays.flat().every((d) => d.delayTime.events.length === 0));
		k.setTime(BEAT_SEC / 2, 6);
		k.setTime(BEAT_SEC / 4, 6.01);
		k.setTime(BEAT_SEC / 3, 6.02);
		assert.equal(ctx.created.length, built);
		const xfade = m.MULTITAP_RETUNE_XFADE_SEC;
		assert.equal(xfade, 0.03);
		assert.deepEqual(
			k.delays[1].map((d) => d.delayTime.events),
			k.delays[1].map((_, i) => [{ kind: 'set', v: ((i + 1) * (BEAT_SEC / 2)) / 8, t: 6 }])
		);
		assert.deepEqual(
			k.delays[0].map((d) => d.delayTime.events),
			k.delays[0].map((_, i) => [{ kind: 'set', v: ((i + 1) * (BEAT_SEC / 3)) / 8, t: 6 + xfade }])
		);
		assert.equal(k.time(), BEAT_SEC / 3);
		for (const [b, bank] of k.delays.entries()) {
			for (const e of bank.flatMap((d) => d.delayTime.events)) {
				assert.equal(e.kind, 'set');
				assert.equal(k.bankGains[b].gain.valueAt(e.t), 0, `bank ${b} silent at its step ${e.t}`);
			}
		}
		for (let t = 6; t < 6.1; t += 0.001) {
			const sum = k.bankGains[0].gain.valueAt(t) + k.bankGains[1].gain.valueAt(t);
			assert.ok(Math.abs(sum - 1) < 1e-9, `banks sum to 1 at ${t}`);
		}
	});

	test('[if] the flat or decay tap gain table drifts from wet or wet * 0.85^(k-1) [then] the law is not the one rated, [else stop]', () => {
		assert.equal(m.MULTITAP_DECAY, 0.85);
		assert.equal(m.MULTITAP_LAW_DEFAULT, 'decay');
		assert.deepEqual(multitapTapGains('flat', 7, 0.6), [0.6, 0.6, 0.6, 0.6, 0.6, 0.6, 0.6]);
		const decay = [0.6, 0.51, 0.4335, 0.368475, 0.31320375, 0.2662231875, 0.226289709375];
		multitapTapGains('decay', 7, 0.6).forEach((g, i) =>
			assert.ok(Math.abs(g - decay[i]) < 1e-12, `tap ${i + 1}`)
		);
		assert.deepEqual(multitapTapGains('flat', 3, 1), [1, 1, 1, 0, 0, 0, 0]);
		for (const law of ['flat', 'decay']) {
			const { k } = build({ multitapLaw: law, wet: 0.6 });
			const table = multitapTapGains(law, 7, 0.6);
			for (const bank of k.tapGains)
				assert.deepEqual(
					bank.map((t) => t.gain.value),
					table
				);
		}
		assert.throws(
			() => build({ multitapLaw: 'ramp' }),
			/multitap law ramp is not one of flat, decay/
		);
	});

	test('[if] an impulse at 124 BPM, 1 beat, 7 taps misses k * time / 8 by 2 ms or rings 50 ms after tap 7 [then] taps are off, [else stop]', () => {
		const { ctx, k } = build();
		const out = render(ctx, k, impulse(1), 1);
		const found = onsets(out);
		assert.equal(found.length, 7);
		const gains = multitapTapGains('decay', 7, 1);
		found.forEach((o, i) => {
			assert.ok(Math.abs(o.at / SR - ((i + 1) * BEAT_SEC) / 8) <= 0.002, `tap ${i + 1} at ${o.at}`);
			assert.ok(Math.abs(o.sum - gains[i] * multitapSumGain(7)) < 1e-9, `tap ${i + 1} level`);
		});
		const after = Math.round(((7 * BEAT_SEC) / 8 + 0.05) * SR);
		assert.ok(peak(out.subarray(after)) < 10 ** (-90 / 20));
		const r = build();
		const half = onsets(
			render(r.ctx, r.k, impulse(1, 0.2), 1, [[0.05, (kk) => kk.setTime(BEAT_SEC / 2, 0.05)]])
		);
		assert.equal(half.length, 7);
		half.forEach((o, i) =>
			assert.ok(Math.abs(o.at / SR - 0.2 - ((i + 1) * BEAT_SEC) / 16) <= 0.002)
		);
	});

	test('[if] 7 flat taps at wet 1 of an in-phase kick go over 0 dBFS [then] the sum gain is wrong, [else stop]', () => {
		assert.equal(multitapSumGain(7), 1 / Math.sqrt(7));
		const { ctx, k } = build({ multitapLaw: 'flat' });
		assert.equal(k.output.gain.value, 1 / Math.sqrt(7));
		const out = render(ctx, k, inPhaseKick(), 1);
		const plain = peak(out) * Math.sqrt(7);
		assert.ok(plain > 1, `the plain sum ${plain} would clip, so the sum gain is needed`);
		assert.ok(peak(out) <= 1, `peak ${peak(out)}`);
	});

	test('[if] wet 0 changes dry, or any render has NaN, infinity or a subnormal [then] MULTI TAP is not safe, [else stop]', () => {
		const sine = Float64Array.from({ length: SR }, (_, i) =>
			Math.sin((2 * Math.PI * 440 * i) / SR)
		);
		const silent = build({ wet: 0 });
		const wet0 = render(silent.ctx, silent.k, sine, 1, [
			[0.3, (k) => k.setTime(BEAT_SEC / 2, 0.3)]
		]);
		assert.ok(sine.every((v, i) => v + wet0[i] === v));
		const { ctx, k } = build({ wet: 0.8 });
		const out = render(ctx, k, sine, 1.2, [
			[0.2, (kk) => kk.setTime(BEAT_SEC / 4, 0.2)],
			[0.21, (kk) => kk.setTime(BEAT_SEC, 0.21)],
			[0.4, (kk) => kk.setTaps(2, 0.4)],
			[0.5, (kk) => kk.setWet(0.3, 0.5)],
			[0.6, (kk) => kk.gateInput(false, 0.6)]
		]);
		const bound = multitapTapGains('decay', 7, 0.8).reduce((s, g) => s + g, 0) * multitapSumGain(7);
		for (const v of out) {
			assert.ok(Number.isFinite(v));
			assert.ok(v === 0 || Math.abs(v) >= FLT_MIN_NORMAL);
			assert.ok(Math.abs(v) <= bound);
		}
		assert.equal(peak(out.subarray(Math.round(1.1 * SR))), 0);
	});
});
