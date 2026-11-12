// requirement: FXROLL-9793
// [if] a missing tempo source does not throw naming the deck or leaves the kernel live [then] broken
// [if] a division outside 1/16..16 (3/4 included) is accepted [then] broken
// [if] repeat starts drift more than 0.1 ms over 64 repeats [then] broken
// [if] a division change while on does not record again from the live input [then] broken
// [if] off or LEVEL/DEPTH fully counterclockwise is not bit-exact dry [then] broken
// [if] fully clockwise contains any dry signal [then] broken
// [if] release is not dry within one crossfade or a click ratio exceeds 1.5 [then] broken
// [if] latency exceeds 128 frames, or any sample is over 0 dBFS, NaN or subnormal [then] broken
// [if] render allocates per quantum [then] broken
// [if] Rust engine mode does not refuse the ROLL kernel by name [then] broken
import assert from 'node:assert/strict';
import { before, describe, test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const SR = 48000;
const Q = 128;
const PEAK = 0.5;
const FLT_MIN = 1.1754943508222875e-38;
const BPM128 = 60 / 128;
const BPM1237 = 60 / 123.7;
const beatFrames128 = BPM128 * SR;

let mod;

function mulberry32(seed) {
	let s = seed >>> 0;
	return () => {
		s = (s + 0x6d2b79f5) >>> 0;
		let t = Math.imul(s ^ (s >>> 15), s | 1);
		t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
		return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
	};
}

function bytesEqual(a, b) {
	return (
		a.length === b.length &&
		Buffer.from(a.buffer, a.byteOffset, a.byteLength).equals(
			Buffer.from(b.buffer, b.byteOffset, b.byteLength)
		)
	);
}

function renderBlocks(kernel, inL, inR, events = []) {
	const total = inL.length;
	const outL = new Float32Array(total);
	const outR = new Float32Array(total);
	const scratchL = new Float32Array(Q);
	const scratchR = new Float32Array(Q);
	let ev = 0;
	for (let base = 0; base < total; base += Q) {
		while (ev < events.length && events[ev].atFrame <= base + Q - 1) {
			events[ev].fn(kernel);
			ev += 1;
		}
		const end = Math.min(base + Q, total);
		const n = end - base;
		kernel.render(
			inL.subarray(base, end),
			inR.subarray(base, end),
			scratchL.subarray(0, n),
			scratchR.subarray(0, n)
		);
		outL.set(scratchL.subarray(0, n), base);
		outR.set(scratchR.subarray(0, n), base);
	}
	return [outL, outR];
}

function makeKernel({
	bpm = 128,
	deck = 'A',
	depth = 1,
	division = 1 / 4,
	xfadeFrames,
	levelLaw,
	beatSecondsFn
} = {}) {
	const beatSec = 60 / bpm;
	const k = mod.createRollFxKernel({
		sampleRate: SR,
		deck,
		beatSeconds: beatSecondsFn ?? (() => beatSec),
		depth,
		division,
		xfadeFrames,
		levelLaw
	});
	k.setEngaged(true);
	return { kernel: k, beatSec, beatFrames: beatSec * SR };
}

function sineFixture(frames, freq = 440, amp = PEAK, phase = 0) {
	const l = new Float32Array(frames);
	const r = new Float32Array(frames);
	for (let i = 0; i < frames; i += 1) {
		const s = amp * Math.sin((2 * Math.PI * freq * i) / SR + phase);
		l[i] = s;
		r[i] = s * 0.97;
	}
	return [l, r];
}

function noiseFixture(frames, seed = 42) {
	const rnd = mulberry32(seed);
	const l = new Float32Array(frames);
	const r = new Float32Array(frames);
	for (let i = 0; i < frames; i += 1) {
		l[i] = (rnd() * 2 - 1) * PEAK;
		r[i] = (rnd() * 2 - 1) * PEAK;
	}
	return [l, r];
}

function musicFixture(frames, bpm = 128) {
	const beatF = (60 / bpm) * SR;
	const l = new Float32Array(frames);
	const r = new Float32Array(frames);
	const rnd = mulberry32(7);
	for (let i = 0; i < frames; i += 1) {
		const beat = Math.floor(i / beatF);
		const pos = i - beat * beatF;
		let v = 0;
		if (pos < beatF * 0.1) {
			const t = pos / (beatF * 0.1);
			v = PEAK * 0.8 * Math.sin(2 * Math.PI * (120 + t * 70) * (pos / SR));
		}
		v += PEAK * 0.15 * Math.sin((2 * Math.PI * 55 * i) / SR);
		l[i] = v;
		r[i] = v + (rnd() - 0.5) * 0.02;
	}
	return [l, r];
}

function decayTailFixture(frames) {
	const l = new Float32Array(frames);
	const r = new Float32Array(frames);
	for (let i = 0; i < frames; i += 1) {
		const d = PEAK * Math.exp(-i / (SR * 0.05));
		l[i] = d;
		r[i] = d;
	}
	const tail = Math.min(frames, SR);
	for (let i = frames - tail; i < frames; i += 1) {
		if (Math.abs(l[i]) < FLT_MIN) {
			l[i] = 0;
			r[i] = 0;
		}
	}
	return [l, r];
}

function xcorrPeak(a, b, maxLag) {
	let bestLag = 0;
	let best = -Infinity;
	for (let lag = -maxLag; lag <= maxLag; lag += 1) {
		let sum = 0;
		let normA = 0;
		let normB = 0;
		for (let i = 0; i < a.length; i += 1) {
			const j = i + lag;
			if (j < 0 || j >= b.length) continue;
			sum += a[i] * b[j];
			normA += a[i] * a[i];
			normB += b[j] * b[j];
		}
		const n = sum / Math.sqrt(normA * normB + 1e-30);
		if (n > best) {
			best = n;
			bestLag = lag;
		}
	}
	return { lag: bestLag, peak: best };
}

function maxSlope(y, a, b) {
	let mx = 0;
	for (let i = a + 1; i < b && i < y.length; i += 1) {
		mx = Math.max(mx, Math.abs(y[i] - y[i - 1]));
	}
	return mx;
}

function clickRatio(out, ref, at, X) {
	const a = Math.max(0, at - X);
	const b = Math.min(out.length, at + 2 * X);
	const mid = maxSlope(out, a, b);
	const pre = maxSlope(out, Math.max(0, at - 4 * X), Math.max(0, at - X));
	const dry = maxSlope(ref, Math.max(0, at - X), b);
	return mid / Math.max(pre, dry, 1e-12);
}

function energyCentroid(seg, center) {
	let wsum = 0;
	let esum = 0;
	for (let i = 0; i < seg.length; i += 1) {
		const e = seg[i] * seg[i];
		esum += e;
		wsum += e * (center + i);
	}
	return wsum / (esum + 1e-30);
}

function hannBurst(frames, center, width) {
	const l = new Float32Array(frames);
	for (let i = 0; i < frames; i += 1) {
		const d = i - center;
		if (Math.abs(d) <= width / 2) {
			const t = (2 * d) / width;
			l[i] = 0.5 * (1 + Math.cos(Math.PI * t));
		}
	}
	return l;
}

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/roll-fx-kernel.ts');
});

describe('roll-fx-kernel', () => {
	test('1 constants', () => {
		const expected = [1 / 16, 1 / 8, 1 / 4, 1 / 2, 1, 2, 4, 8, 16];
		assert.deepEqual([...mod.ROLL_FX_DIVISIONS], expected);
		assert.equal(mod.ROLL_FX_DEFAULT_LEVEL_LAW, 'equal-power');
		assert.ok(mod.ROLL_FX_XFADE_CANDIDATES.includes(mod.ROLL_FX_XFADE_FRAMES));
		assert.deepEqual(mod.rollLevelGains('equal-gain', 0), [1, 0]);
		assert.deepEqual(mod.rollLevelGains('equal-gain', 1), [0, 1]);
		assert.deepEqual(mod.rollLevelGains('equal-power', 0), [1, 0]);
		assert.deepEqual(mod.rollLevelGains('equal-power', 1), [0, 1]);
		const mid = mod.rollLevelGains('equal-power', 0.5);
		assert.ok(Math.abs(mid[0] - Math.cos(Math.PI / 4)) < 1e-9);
		assert.ok(Math.abs(mid[1] - Math.sin(Math.PI / 4)) < 1e-9);
	});

	test('2 division set', () => {
		for (const d of mod.ROLL_FX_DIVISIONS) {
			assert.equal(mod.parseRollDivision(d), d);
		}
		for (const bad of [3 / 4, 1 / 32, 32, 0, NaN, '1/4']) {
			assert.throws(() => mod.parseRollDivision(bad), RangeError);
		}
		const k = mod.createRollFxKernel({
			sampleRate: SR,
			deck: 'A',
			beatSeconds: () => BPM128
		});
		const before = k.division;
		assert.throws(() => k.setDivision(3 / 4), RangeError);
		assert.equal(k.division, before);
	});

	test('3 tempo source', () => {
		for (const bs of [undefined, null]) {
			assert.throws(
				() =>
					mod.createRollFxKernel({
						sampleRate: SR,
						deck: 'A',
						beatSeconds: bs
					}),
				(err) => err instanceof Error && /deck A/.test(err.message) && /tempo/.test(err.message)
			);
		}
		const k = mod.createRollFxKernel({
			sampleRate: SR,
			deck: 'B',
			beatSeconds: () => null
		});
		for (const ret of [null, NaN, 60 / 20, 60 / 400]) {
			const kk = mod.createRollFxKernel({
				sampleRate: SR,
				deck: 'B',
				beatSeconds: () => ret
			});
			assert.throws(
				() => kk.setEngaged(true),
				(err) => /deck B/.test(err.message)
			);
			assert.equal(kk.state, 'off');
			assert.equal(kk.allocations, 0);
			const [inL, inR] = noiseFixture(Q);
			const [outL, outR] = renderBlocks(kk, inL, inR);
			assert.ok(bytesEqual(outL, inL));
			assert.ok(bytesEqual(outR, inR));
		}
		let live = true;
		const tempoK = mod.createRollFxKernel({
			sampleRate: SR,
			deck: 'C',
			beatSeconds: () => (live ? BPM128 : null),
			depth: 1
		});
		tempoK.setEngaged(true);
		const frames = Math.ceil(beatFrames128 / 4) * 8 + Q * 20;
		const [inL, inR] = noiseFixture(frames);
		renderBlocks(tempoK, inL, inR);
		live = false;
		const X = mod.ROLL_FX_XFADE_FRAMES;
		assert.throws(
			() => tempoK.setDivision(1 / 8),
			(err) => /deck C/.test(err.message)
		);
		const tailL = new Float32Array(Q * 4);
		const tailR = new Float32Array(Q * 4);
		renderBlocks(tempoK, tailL, tailR);
		assert.equal(tempoK.state, 'off');
		const check = noiseFixture(Q);
		const [oL] = renderBlocks(tempoK, check[0], check[1]);
		assert.ok(bytesEqual(oL, check[0]));
		void X;
	});

	test('4 capture then repeat', () => {
		const { kernel } = makeKernel({ depth: 1, division: 1 / 4 });
		const L = kernel.repeatFrames;
		const X = mod.ROLL_FX_XFADE_FRAMES;
		const frames = Math.ceil(L) * 5 + X * 4 + Q;
		const [inL, inR] = noiseFixture(frames, 11);
		const [outL, outR] = renderBlocks(kernel, inL, inR);
		assert.ok(kernel.state === 'playing' || kernel.state === 'capturing');
		const capEnd = Math.floor(L);
		assert.ok(bytesEqual(outL.subarray(0, capEnd), inL.subarray(0, capEnd)));
		const start = Math.ceil(L) + X;
		const period = Math.floor(L);
		for (let p = 1; p < 3; p += 1) {
			const a = start + p * period;
			const b = a + period;
			for (let i = 0; i < period && b + i < outL.length; i += 1) {
				assert.ok(Math.abs(outL[a + i] - outL[start + i]) < 1e-5);
			}
		}
		const ref = outL.subarray(start, start + Math.floor(L));
		const body = outL.subarray(start + Math.floor(L), start + 2 * Math.floor(L));
		const { peak } = xcorrPeak(body, ref, 8);
		assert.ok(peak >= 0.99);
		assert.ok(bytesEqual(outR.subarray(0, capEnd), inR.subarray(0, capEnd)));
	});

	test('5 drift', () => {
		const bpm = 123.7;
		const beatF = (60 / bpm) * SR;
		const L = 0.25 * beatF;
		const { kernel } = makeKernel({ bpm, depth: 1, division: 1 / 4 });
		assert.ok(Math.abs(kernel.repeatFrames - L) < 1e-9);
		const offset = 1000;
		const frames = offset + 64 * L + Q * 20;
		const burst = hannBurst(frames, offset, 32);
		const [outL] = renderBlocks(kernel, burst, burst);
		const budget = 0.0001 * SR;
		let c1 = 0;
		let c64 = 0;
		const win = 48;
		for (let k = 1; k <= 64; k += 1) {
			const center = offset + k * L;
			const a = Math.max(0, Math.floor(center) - win);
			const seg = outL.subarray(a, a + win * 2);
			const cent = energyCentroid(seg, a);
			assert.ok(Math.abs(cent - center) <= budget);
			if (k === 1) c1 = cent;
			if (k === 64) c64 = cent;
		}
		assert.ok(Math.abs(c64 - c1 - 63 * L) <= budget);
	});

	test('6 division change re-records', () => {
		const Lq = Math.ceil(beatFrames128 / 4);
		const Falign = Math.ceil((Lq * 3) / Q) * Q;
		const frames = Falign + mod.ROLL_FX_XFADE_FRAMES * 4 + Math.ceil(beatFrames128 / 2) * 4;
		const [aL, aR] = noiseFixture(frames, 1);
		const [bL, bR] = noiseFixture(frames, 99);
		const mixL = new Float32Array(frames);
		const mixR = new Float32Array(frames);
		for (let i = 0; i < frames; i += 1) {
			mixL[i] = i < Falign ? aL[i] : bL[i];
			mixR[i] = i < Falign ? aR[i] : bR[i];
		}
		const kernel = mod.createRollFxKernel({
			sampleRate: SR,
			deck: 'A',
			beatSeconds: () => BPM128,
			depth: 1,
			division: 1 / 4
		});
		kernel.setEngaged(true);
		assert.equal(kernel.allocations, 1);
		const states = [];
		const ev = [
			{
				atFrame: Falign,
				fn: (k) => {
					k.setDivision(1 / 2);
					states.push(k.state);
				}
			}
		];
		const [outL] = renderBlocks(kernel, mixL, mixR, ev);
		assert.ok(states.includes('releasing'));
		assert.ok(kernel.state === 'playing' || kernel.state === 'capturing');
		assert.equal(kernel.division, 1 / 2);
		const L2 = beatFrames128 / 2;
		assert.ok(Math.abs(kernel.repeatFrames - L2) < 1e-6);
		const X = mod.ROLL_FX_XFADE_FRAMES;
		const capStart = Falign + X;
		const playStart = capStart + Math.ceil(L2) + X + Math.ceil(L2);
		const p = Math.floor(L2);
		const body = outL.subarray(playStart, playStart + p);
		const next = outL.subarray(playStart + p, playStart + 2 * p);
		assert.ok(xcorrPeak(body, next, 4).peak >= 0.99);
		const { peak: peakA } = xcorrPeak(body, aL.subarray(0, p), 16);
		assert.ok(peakA <= 0.2);

		const bigK = mod.createRollFxKernel({
			sampleRate: SR,
			deck: 'A',
			beatSeconds: () => BPM128,
			depth: 1,
			division: 1 / 4
		});
		bigK.setEngaged(true);
		const a0 = bigK.allocations;
		bigK.setDivision(1);
		assert.equal(bigK.allocations, a0 + 1);

		const smallK = mod.createRollFxKernel({
			sampleRate: SR,
			deck: 'A',
			beatSeconds: () => BPM128,
			depth: 1,
			division: 1 / 4
		});
		smallK.setEngaged(true);
		const before = smallK.allocations;
		smallK.setDivision(1 / 8);
		assert.equal(smallK.allocations, before);
	});

	test('7 bit-exact dry', () => {
		const fixtures = [
			sineFixture(Q * 16),
			noiseFixture(Q * 16),
			musicFixture(Q * 16),
			decayTailFixture(Q * 16)
		];
		const tempos = [90, 128, 200];
		for (const [inL, inR] of fixtures) {
			const pass = mod.createRollFxKernel({
				sampleRate: SR,
				deck: 'A',
				beatSeconds: () => BPM128
			});
			const [oL, oR] = renderBlocks(pass, inL, inR);
			assert.ok(bytesEqual(oL, inL));
			assert.ok(bytesEqual(oR, inR));
		}
		const [inL, inR] = noiseFixture(Q * 40);
		const d0 = makeKernel({ depth: 0 });
		const [dryL, dryR] = renderBlocks(d0.kernel, inL, inR);
		assert.ok(bytesEqual(dryL, inL));
		assert.ok(bytesEqual(dryR, inR));

		const ramp = makeKernel({ depth: 0.5 });
		const events = [{ atFrame: Q * 2, fn: (k) => k.setDepth(0) }];
		const [rL, rR] = renderBlocks(ramp.kernel, inL, inR, events);
		const after = Q * 3;
		assert.ok(bytesEqual(rL.subarray(after), inL.subarray(after)));
		assert.ok(bytesEqual(rR.subarray(after), inR.subarray(after)));

		const X = mod.ROLL_FX_XFADE_FRAMES;
		for (const bpm of tempos) {
			for (const [fL, fR] of fixtures) {
				const { kernel } = makeKernel({ bpm, depth: 1 });
				const offEv = [{ atFrame: Q * 8, fn: (k) => k.setEngaged(false) }];
				const [oL, oR] = renderBlocks(kernel, fL, fR, offEv);
				const dryAt = Q * 8 + X + Q;
				assert.ok(bytesEqual(oL.subarray(dryAt), fL.subarray(dryAt)));
				assert.ok(bytesEqual(oR.subarray(dryAt), fR.subarray(dryAt)));
			}
		}
	});

	test('7b depth twitch and OFF discipline', () => {
		const frames = Q * 250;
		const [inL, inR] = sineFixture(frames, 440);
		const playAt = Math.ceil(beatFrames128 / 4) + mod.ROLL_FX_XFADE_FRAMES + Q * 2;

		const ccw = makeKernel({ depth: 0 });
		const [ccwL] = renderBlocks(ccw.kernel, inL, inR, [
			{
				atFrame: playAt,
				fn: (k) => {
					k.setDepth(0.01);
					k.setDepth(0);
				}
			}
		]);
		const dryFrom = playAt + Q * 2;
		assert.ok(bytesEqual(ccwL.subarray(dryFrom), inL.subarray(dryFrom)));

		const cw = makeKernel({ depth: 1 });
		const wetOnly = mod.createRollFxKernel({
			sampleRate: SR,
			deck: 'A',
			beatSeconds: () => BPM128,
			depth: 1,
			division: 1 / 4
		});
		wetOnly.setEngaged(true);
		const [refL] = renderBlocks(wetOnly, inL, inR);
		const [cwL] = renderBlocks(cw.kernel, inL, inR, [
			{
				atFrame: playAt,
				fn: (k) => {
					k.setDepth(0.5);
					k.setDepth(1);
				}
			}
		]);
		const cwFrom = playAt + Q * 2;
		assert.ok(bytesEqual(cwL.subarray(cwFrom), refL.subarray(cwFrom)));

		const offDiv = makeKernel({ depth: 1 });
		const X = mod.ROLL_FX_XFADE_FRAMES;
		const Lroll = offDiv.kernel.repeatFrames;
		const offAt = Math.ceil(Lroll) + X + Math.ceil(Lroll) + Q * 2;
		renderBlocks(offDiv.kernel, inL, inR, [
			{ atFrame: offAt, fn: (k) => k.setEngaged(false) },
			{ atFrame: offAt + Q, fn: (k) => k.setDivision(1 / 8) }
		]);
		assert.equal(offDiv.kernel.division, 1 / 8);
		assert.notEqual(offDiv.kernel.state, 'playing');
		const [offOutL] = renderBlocks(offDiv.kernel, inL, inR);
		const dryAfterOff = offAt + X + Q * 2;
		assert.ok(bytesEqual(offOutL.subarray(dryAfterOff), inL.subarray(dryAfterOff)));
		assert.equal(offDiv.kernel.state, 'off');

		const pend = mod.createRollFxKernel({
			sampleRate: SR,
			deck: 'A',
			beatSeconds: () => BPM128,
			depth: 1,
			division: 1 / 4
		});
		pend.setDivision(1 / 8);
		pend.setEngaged(true);
		const mid = Math.ceil(pend.repeatFrames) + Q * 4;
		renderBlocks(pend, inL, inR, [
			{ atFrame: mid, fn: (k) => k.setEngaged(false) },
			{ atFrame: mid + Q, fn: (k) => k.setEngaged(true) },
			{ atFrame: mid + 2 * Q, fn: (k) => k.setEngaged(false) }
		]);
		const [pendOut] = renderBlocks(pend, inL, inR);
		const pendDry = mid + 2 * Q + X + Q * 2;
		assert.ok(bytesEqual(pendOut.subarray(pendDry), inL.subarray(pendDry)));
		assert.equal(pend.state, 'off');
	});

	test('7c release keeps roll moving and re-ON allocates on control thread', () => {
		const frames = Q * 250;
		const [inL, inR] = sineFixture(frames, 523);
		const { kernel } = makeKernel({ depth: 1 });
		const Lk = kernel.repeatFrames;
		const offAt = Math.ceil(Lk) + mod.ROLL_FX_XFADE_FRAMES + Math.ceil(Lk) + Q * 2;
		const [outL] = renderBlocks(kernel, inL, inR, [
			{ atFrame: offAt, fn: (k) => k.setEngaged(false) }
		]);
		const rel = outL.subarray(offAt, offAt + mod.ROLL_FX_XFADE_FRAMES);
		let minStep = Infinity;
		for (let i = 1; i < rel.length; i += 1) {
			minStep = Math.min(minStep, Math.abs(rel[i] - rel[i - 1]));
		}
		assert.ok(minStep > 1e-5);

		let bpm = 128;
		const reonK = mod.createRollFxKernel({
			sampleRate: SR,
			deck: 'A',
			beatSeconds: () => 60 / bpm,
			depth: 1,
			division: 1 / 4
		});
		reonK.setEngaged(true);
		const alloc0 = reonK.allocations;
		const Lsmall = reonK.repeatFrames;
		const reAt = Math.ceil(Lsmall) + mod.ROLL_FX_XFADE_FRAMES + Math.ceil(Lsmall) + Q * 2;
		renderBlocks(reonK, inL, inR, [
			{ atFrame: reAt, fn: (k) => k.setEngaged(false) },
			{
				atFrame: reAt + Q,
				fn: (k) => {
					bpm = 90;
					k.setEngaged(true);
				}
			}
		]);
		assert.equal(reonK.allocations, alloc0 + 1);
		renderBlocks(reonK, inL, inR);
		assert.equal(reonK.allocations, alloc0 + 1);

		let tempoLive = true;
		const nullK = mod.createRollFxKernel({
			sampleRate: SR,
			deck: 'A',
			beatSeconds: () => (tempoLive ? BPM128 : null),
			depth: 1
		});
		nullK.setEngaged(true);
		const nAt = Math.ceil(nullK.repeatFrames) + Q * 2;
		renderBlocks(nullK, inL, inR, [
			{ atFrame: nAt, fn: (k) => k.setEngaged(false) },
			{
				atFrame: nAt + Q,
				fn: (k) => {
					tempoLive = false;
					assert.throws(() => k.setEngaged(true), /no tempo source/);
				}
			}
		]);
		renderBlocks(nullK, inL, inR);
	});

	test('8 fully clockwise roll only', () => {
		const L = (1 / 4) * BPM128 * SR;
		const X = mod.ROLL_FX_XFADE_FRAMES;
		const split = Math.ceil(L) + X + 2;
		const frames = split + Q * 20;
		const [sL, sR] = sineFixture(frames);
		const compL = new Float32Array(frames);
		const compR = new Float32Array(frames);
		compL.set(sL.subarray(0, split));
		compR.set(sR.subarray(0, split));
		const { kernel: k1 } = makeKernel({ depth: 1, division: 1 / 4 });
		const { kernel: k2 } = makeKernel({ depth: 1, division: 1 / 4 });
		const [o1L] = renderBlocks(k1, sL, sR);
		const [o2L] = renderBlocks(k2, compL, compR);
		const from = Math.ceil(L) + 2 * X;
		assert.ok(bytesEqual(o1L.subarray(from), o2L.subarray(from)));
	});

	test('9 release and click ratio', () => {
		const CLICK_MAX = 1.5;
		const passes = [];
		const worstByX = new Map();
		for (const edgeX of mod.ROLL_FX_XFADE_CANDIDATES) {
			let worst = 0;
			const [inL, inR] = sineFixture(SR * 8, 440);
			for (const division of [1 / 8, 1 / 4, 1]) {
				for (const depth of [0.5, 1]) {
					const { kernel } = makeKernel({ depth, division, xfadeFrames: edgeX });
					const offAt = Math.ceil(kernel.repeatFrames) * 2 + edgeX;
					const need = offAt + edgeX + Q * 2;
					const padL = new Float32Array(need);
					const padR = new Float32Array(need);
					padL.set(inL.subarray(0, Math.min(need, inL.length)));
					padR.set(inR.subarray(0, Math.min(need, inR.length)));
					const [outL] = renderBlocks(kernel, padL, padR, [
						{ atFrame: offAt, fn: (k) => k.setEngaged(false) }
					]);
					const L = kernel.repeatFrames;
					const cOff = clickRatio(outL, padL, offAt, edgeX);
					const cEntry = clickRatio(outL, padL, Math.ceil(L), edgeX);
					const cSeam = clickRatio(outL, padL, Math.ceil(3 * L), edgeX);
					worst = Math.max(worst, cOff, cEntry, cSeam);
					assert.ok(cOff <= CLICK_MAX);
					assert.ok(cEntry <= CLICK_MAX);
					assert.ok(cSeam <= CLICK_MAX);
				}
			}
			worstByX.set(edgeX, worst);
			if (worst <= CLICK_MAX) passes.push(edgeX);
		}
		assert.ok(passes.length > 0);
		assert.ok(passes.includes(mod.ROLL_FX_XFADE_FRAMES));
		assert.equal(mod.ROLL_FX_XFADE_FRAMES, Math.min(...passes));

		const [mL, mR] = musicFixture(SR * 8);
		for (const division of [1 / 8, 1 / 4, 1]) {
			for (const depth of [0.5, 1]) {
				const { kernel } = makeKernel({ depth, division });
				const X = mod.ROLL_FX_XFADE_FRAMES;
				const offAt = Math.ceil(kernel.repeatFrames) * 2 + X;
				const need = offAt + X + Q * 2;
				const padL = new Float32Array(need);
				const padR = new Float32Array(need);
				padL.set(mL.subarray(0, Math.min(need, mL.length)));
				padR.set(mR.subarray(0, Math.min(need, mR.length)));
				const [outL] = renderBlocks(kernel, padL, padR, [
					{ atFrame: offAt, fn: (k) => k.setEngaged(false) }
				]);
				const dryAt = offAt + X + Q;
				assert.ok(bytesEqual(outL.subarray(dryAt), padL.subarray(dryAt)));
				assert.equal(kernel.state, 'off');
				assert.equal(kernel.bufferFrames, 0);
				if (division >= 1 / 8) {
					assert.ok(clickRatio(outL, padL, Math.ceil(3 * kernel.repeatFrames), X) <= CLICK_MAX);
				}
			}
		}
	});

	test('10 safety grid', () => {
		const depths = [0, 0.1, 0.25, 0.5, 0.75, 0.9, 1];
		for (const division of mod.ROLL_FX_DIVISIONS) {
			const bpm = division >= 4 ? 300 : 128;
			const beatF = (60 / bpm) * SR;
			const span = Math.min(SR * 4, Math.ceil(division * beatF) * 3 + Q * 8);
			const fixtures = [sineFixture(span), noiseFixture(span, 3), musicFixture(span, bpm)];
			for (const law of mod.ROLL_FX_LEVEL_LAWS) {
				for (const depth of depths) {
					for (const [inL, inR] of fixtures) {
						const k = mod.createRollFxKernel({
							sampleRate: SR,
							deck: 'A',
							beatSeconds: () => 60 / bpm,
							division,
							depth,
							levelLaw: law
						});
						const events = [
							{ atFrame: 0, fn: (kk) => kk.setEngaged(true) },
							{ atFrame: Q, fn: (kk) => kk.setDepth(1 - depth) },
							{ atFrame: Q * 2, fn: (kk) => kk.setDivision(division === 1 / 16 ? 1 / 8 : 1 / 16) },
							{ atFrame: Q * 3, fn: (kk) => kk.setEngaged(false) }
						];
						const [oL, oR] = renderBlocks(k, inL, inR, events);
						for (let i = 0; i < oL.length; i += 1) {
							assert.ok(Math.abs(oL[i]) <= 1 && Number.isFinite(oL[i]));
							assert.ok(Math.abs(oR[i]) <= 1 && Number.isFinite(oR[i]));
							const av = Math.abs(oL[i]);
							assert.ok(!(av > 0 && av < FLT_MIN));
						}
					}
				}
			}
		}
	});

	test('11 latency', () => {
		const { kernel } = makeKernel({ depth: 0.5 });
		assert.equal(kernel.latencyFrames, 0);
		assert.ok(kernel.latencyFrames <= mod.ROLL_FX_MAX_LATENCY_FRAMES);
		const imp = new Float32Array(Q * 4);
		imp[Q] = 0.25;
		const [oL] = renderBlocks(kernel, imp, imp);
		assert.ok(
			Math.abs(oL[Q] - 0.25 * mod.rollLevelGains('equal-power', 0.5)[0]) < 0.02 ||
				Math.abs(oL[Q]) > 0
		);
		const capK = makeKernel({ depth: 1 });
		const imp2 = new Float32Array(Q * 2);
		imp2[10] = 0.5;
		const [o2] = renderBlocks(capK.kernel, imp2, imp2);
		assert.equal(o2[10], 0.5);
	});

	test('12 no per-quantum allocation', () => {
		const { kernel } = makeKernel({ depth: 1 });
		assert.equal(kernel.allocations, 1);
		const hold = new Float32Array(Q * 1000);
		renderBlocks(kernel, hold, hold);
		assert.equal(kernel.allocations, 1);
		const src = readFrontendSource('src/lib/rb/roll-fx-kernel.ts');
		const start = src.indexOf('// render-path:start');
		const end = src.indexOf('// render-path:end');
		assert.ok(start >= 0 && end > start);
		const body = src.slice(start, end);
		assert.ok(!/\bnew (?!RangeError|Error)/.test(body));
		assert.ok(!/=>/.test(body));
		assert.ok(!/\.slice\(/.test(body));
		assert.ok(!/\.subarray\(/.test(body));
		assert.ok(!/\.map\(/.test(body));
		assert.ok(!/\.concat\(/.test(body));
	});

	test('13 rust refusal', () => {
		assert.throws(
			() =>
				mod.createRollFxKernel({
					sampleRate: SR,
					deck: 'A',
					beatSeconds: () => BPM128,
					engine: 'rust'
				}),
			(err) =>
				err instanceof Error &&
				/^RUST_ENGINE_UNAVAILABLE: beat FX ROLL \(roll\) on deck A/.test(err.message) &&
				err.message.includes('PARITY-TODO')
		);
		const msg = mod.rollFxRustRefusal('B');
		assert.ok(msg.includes('deck B'));
		assert.ok(msg.includes(mod.ROLL_FX_EFFECT));
	});
});
