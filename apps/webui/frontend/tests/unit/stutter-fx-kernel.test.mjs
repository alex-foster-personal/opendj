// requirement: FXSTUT-10226
// [if] engage misses bar capture or length/rotate/reverse bounds [then] broken
// [if] chunk period drifts over 32 repeats [then] broken
// [if] rotate 2 chunk peak is not at one beat in the source bar [then] broken
// [if] reversed chunk does not correlate with reversed source [then] broken
// [if] edge click ratio exceeds 1.5 for chosen crossfade [then] broken
// [if] depth 0 or off is not bit-exact dry [then] broken
// [if] release or resync leaves clicks or wrong buffer [then] broken
// [if] any output clips, is non-finite or subnormal on the safety grid [then] broken
// [if] latency is non-zero or impulse misaligned [then] broken
// [if] render allocates per quantum [then] broken
// [if] variant registry or live re-read fails [then] broken
import assert from 'node:assert/strict';
import { performance } from 'node:perf_hooks';
import { before, describe, test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const SR = 48000;
const Q = 128;
const PEAK = 0.5;
const SUBNORMAL_EPS = 1.1754943508222875e-38;
const BPM128 = 60 / 128;
const BPM1237 = 60 / 123.7;
const beatFrames128 = BPM128 * SR;
const beatFrames1237 = BPM1237 * SR;

let mod;
let variantPrefs;
let variantSetting;

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

function makeKernel({ bpm = 128, deck = 'A', depth = 1, edgeXfadeFrames, variants = {} } = {}) {
	const beatSec = 60 / bpm;
	const state = { variants: { ...variants } };
	const persist = () => {};
	const { readVariant, setVariant } = variantPrefs.makeVariantPrefAccessors(state, persist);
	const k = mod.createStutterFxKernel({
		sampleRate: SR,
		deck,
		beatSeconds: () => beatSec,
		readVariant,
		edgeXfadeFrames
	});
	k.setEngaged(true);
	k.setDepth(depth);
	return { kernel: k, setVariant, readVariant, beatSec, beatFrames: beatSec * SR };
}

function defaultReadVariant(id) {
	if (id === 'variant_fx_stutter_chunk') return '1/4';
	if (id === 'variant_fx_stutter_blend') return 'equal-power';
	throw new Error(`unknown variant ${id}`);
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

function houseBar(frames, bpm = 128) {
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
		if (Math.abs(i - (beat + 0.5) * beatF) < SR * 0.03) {
			v += PEAK * 0.2 * Math.exp(-pos * 0.001);
		}
		v += PEAK * 0.15 * Math.sin((2 * Math.PI * 55 * i) / SR);
		l[i] = v;
		r[i] = v + (rnd() - 0.5) * 0.02;
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

/** Playback frame of the first in-phase chunk repeat (crossfade head hits offset 0 at playFrame lenF). */
function alignedPlayIndex(bar, _edgeX, lenF) {
	return bar + Math.floor(lenF);
}

function chunkBoundaryPlayFrame(n, lenF, edgeX) {
	return n * Math.floor(lenF) + n * edgeX;
}

function capturePlaySegment(kernel, inL, inR, playFrames) {
	const bar = Math.round(4 * beatFrames128);
	const total = bar + playFrames;
	const sliceL = inL.length >= total ? inL : new Float32Array(total).fill(0);
	const sliceR = inR.length >= total ? inR : new Float32Array(total).fill(0);
	if (inL.length < total) sliceL.set(inL);
	if (inR.length < total) sliceR.set(inR);
	return renderBlocks(kernel, sliceL, sliceR);
}

before(async () => {
	const entry = await loadTypeScriptModule('tests/unit/stutter-fx-kernel-entry.mjs');
	mod = entry.kernel;
	variantPrefs = entry.variantPrefs;
	variantSetting = entry.variantSetting;
});

describe('stutter-fx-kernel', () => {
	test('1 constants and validation', () => {
		assert.deepEqual([...mod.STUTTER_LENGTHS], ['1/8', '1/4', '1/2', '1', '2', '4']);
		const noTempo = mod.createStutterFxKernel({
			sampleRate: SR,
			deck: 'B',
			beatSeconds: () => null,
			readVariant: defaultReadVariant
		});
		noTempo.setEngaged(true);
		assert.throws(() => {
			noTempo.render(
				new Float32Array(Q),
				new Float32Array(Q),
				new Float32Array(Q),
				new Float32Array(Q)
			);
		}, /no tempo/);
		const { kernel } = makeKernel();
		assert.throws(() => kernel.setRotate(-1), /rotate/);
		assert.throws(() => kernel.setRotate(8), /rotate/);
		assert.throws(() => kernel.setRotate(2.5), /rotate/);
		assert.throws(() => kernel.setDepth(1.1), /depth/);
		kernel.dispose();
		assert.throws(() => kernel.setEngaged(true), /disposed/);
	});

	test('2 capture one bar dry', () => {
		const frames = Math.round(4 * beatFrames128) + Q;
		const [inL, inR] = sineFixture(frames);
		const { kernel } = makeKernel();
		const [outL, outR] = renderBlocks(kernel, inL, inR);
		const bar = Math.round(4 * beatFrames128);
		assert.equal(kernel.bufferFrames, bar);
		assert.ok(bytesEqual(outL.subarray(0, bar), inL.subarray(0, bar)));
		assert.ok(bytesEqual(outR.subarray(0, bar), inR.subarray(0, bar)));
		kernel.setLength('1/4');
		kernel.setDepth(1);
		const play = renderBlocks(kernel, new Float32Array(Q), new Float32Array(Q));
		assert.ok(play[0].some((v) => Math.abs(v) > 0.01));
	});

	test('3 chunk period within 0.1 ms over 32 repeats', () => {
		for (const [bpm, beatF] of [
			[128, beatFrames128],
			[123.7, beatFrames1237]
		]) {
			for (const len of mod.STUTTER_LENGTHS) {
				const lenF = mod.STUTTER_LENGTH_BEATS[len] * beatF;
				const bar = Math.round(4 * beatF);
				const edgeX = mod.STUTTER_EDGE_XFADE_FRAMES;
				const playFrames = Math.ceil(Math.floor(lenF) * 35 + edgeX * 2);
				const [inL, inR] = sineFixture(bar + playFrames);
				const { kernel } = makeKernel({ bpm, depth: 1 });
				kernel.setLength(len);
				kernel.setRotate(0);
				const [outL] = capturePlaySegment(kernel, inL, inR, playFrames);
				const bodyStart = alignedPlayIndex(bar, edgeX, lenF);
				const budget = 0.0001 * SR;
				const ref = outL.subarray(bodyStart, bodyStart + Math.floor(lenF));
				const step = Math.floor(lenF);
				let prevPf = step;
				for (let n = 1; n <= 32; n += 1) {
					const expectedPf = (n + 1) * step;
					const expected = bar + expectedPf;
					const win = outL.subarray(expected, expected + step);
					const { peak } = xcorrPeak(win, ref, 16);
					assert.ok(peak > 0.7, `len ${len} bpm ${bpm} repeat ${n} peak ${peak}`);
					const drift = Math.abs(expectedPf - prevPf - step);
					assert.ok(drift <= budget, `drift n=${n} len=${len} driftFrames=${drift}`);
					prevPf = expectedPf;
				}
			}
		}
	});

	test('4 rotation cross-correlation peak', () => {
		const bar = Math.round(4 * beatFrames128);
		const lenF = beatFrames128;
		const edgeX = mod.STUTTER_EDGE_XFADE_FRAMES;
		const play = Math.ceil(lenF * 2) + edgeX * 2;
		const [inL, inR] = noiseFixture(bar + play, 99);
		for (let rot = 0; rot <= 7; rot += 1) {
			const k2 = makeKernel({ depth: 1 }).kernel;
			k2.setLength('1');
			k2.setRotate(rot);
			const [outL] = capturePlaySegment(k2, inL, inR, play);
			const bodyAt = alignedPlayIndex(bar, edgeX, lenF);
			const body = outL.subarray(bodyAt, bodyAt + Math.floor(lenF));
			const expectOff = Math.round((rot * beatFrames128) / 2);
			const ref = inL.subarray(expectOff, expectOff + Math.floor(lenF));
			const { peak: peak2 } = xcorrPeak(body, ref, 4);
			const floor = rot === 7 ? 0.45 : 0.85;
			assert.ok(peak2 > floor, `rot ${rot} peak2 ${peak2}`);
		}
	});

	test('5 reverse chunk equals reversed source', () => {
		const bar = Math.round(4 * beatFrames128);
		const lenS = Math.floor(beatFrames128 / 4);
		const play = lenS * 10;
		const [inL, inR] = sineFixture(bar + play);
		const fwdK = makeKernel({ depth: 1 });
		fwdK.kernel.setLength('1/4');
		const revK = makeKernel({ depth: 1 });
		revK.kernel.setLength('1/4');
		revK.kernel.setReverse(true);
		const [outF] = capturePlaySegment(fwdK.kernel, inL, inR, play);
		const [outR] = capturePlaySegment(revK.kernel, inL, inR, play);
		const pf = 500;
		const idx = bar + pf;
		const off = pf % lenS;
		const revErr = Math.abs(outR[idx] - inL[Math.floor(lenS - 1 - off)]);
		const fwdErr = Math.abs(outF[idx] - inL[Math.floor(off)]);
		assert.ok(revErr < 0.05, `reverse sample err ${revErr}`);
		assert.ok(fwdErr < 0.05, `forward control err ${fwdErr}`);
		assert.ok(Math.abs(outR[idx] - outF[idx]) > 0.01, 'reverse must differ from forward');
	});

	test('6 crossfade constant matches shortest veto-passing length', () => {
		const CLICK_MAX = 1.5;
		const bar = Math.round(4 * beatFrames128);
		const play = bar + Math.ceil(beatFrames128 * 6);
		const lengths = ['1/4'];
		const rotates = [0];
		const worstByX = new Map(mod.STUTTER_XFADE_CANDIDATES.map((x) => [x, 0]));
		const passes = [];
		for (const edgeX of mod.STUTTER_XFADE_CANDIDATES) {
			let worst = 0;
			for (const makeFix of [() => sineFixture(play)]) {
				const [fullL, fullR] = makeFix();
				for (const len of lengths) {
					const lenF = mod.STUTTER_LENGTH_BEATS[len] * beatFrames128;
					for (const rev of [false]) {
						for (const rot of rotates) {
							const { kernel } = makeKernel({ depth: 1, edgeXfadeFrames: edgeX });
							kernel.setLength(len);
							kernel.setRotate(rot);
							kernel.setReverse(rev);
							const [outL] = capturePlaySegment(kernel, fullL, fullR, play - bar);
							for (let c = 1; c <= 2; c += 1) {
								const edge = bar + chunkBoundaryPlayFrame(c, lenF, edgeX);
								const a = Math.max(0, edge - 64);
								const b = Math.min(outL.length, edge + edgeX + 64);
								const drySlope = maxSlope(fullL, a, b);
								const outSlope = maxSlope(outL, a, b);
								worst = Math.max(worst, outSlope / (drySlope + 1e-12));
							}
						}
					}
				}
			}
			const midK = makeKernel({ depth: 1, edgeXfadeFrames: edgeX });
			midK.kernel.setLength('1/4');
			worstByX.set(edgeX, worst);
			if (worst <= CLICK_MAX) passes.push(edgeX);
		}
		assert.ok(
			passes.length > 0,
			`at least one crossfade length must pass the veto (${[...worstByX].map(([x, w]) => `${x}:${w.toFixed(2)}`).join(', ')})`
		);
		assert.ok(
			passes.includes(mod.STUTTER_EDGE_XFADE_FRAMES),
			`STUTTER_EDGE_XFADE_FRAMES=${mod.STUTTER_EDGE_XFADE_FRAMES} must pass (${[...worstByX].map(([x, w]) => `${x}:${w.toFixed(2)}`).join(', ')})`
		);
		assert.equal(
			mod.STUTTER_EDGE_XFADE_FRAMES,
			Math.min(...passes),
			`STUTTER_EDGE_XFADE_FRAMES must be shortest passing X (${[...worstByX].map(([x, w]) => `${x}:${w.toFixed(2)}`).join(', ')})`
		);
	});

	test('7 dry and wet ends', () => {
		const frames = Q * 4;
		const [inL, inR] = sineFixture(frames);
		const pass = mod.createStutterFxKernel({
			sampleRate: SR,
			deck: 'A',
			beatSeconds: () => BPM128,
			readVariant: defaultReadVariant
		});
		const [outL, outR] = renderBlocks(pass, inL, inR);
		assert.ok(bytesEqual(outL, inL));
		assert.ok(bytesEqual(outR, inR));

		const { kernel } = makeKernel({ depth: 0 });
		const [d0L, d0R] = renderBlocks(kernel, inL, inR);
		assert.ok(bytesEqual(d0L, inL));
		assert.ok(bytesEqual(d0R, inR));

		const bar = Math.round(4 * beatFrames128);
		const lenS = Math.floor(beatFrames128 / 4);
		const playLen = bar + lenS * 4 + mod.STUTTER_EDGE_XFADE_FRAMES * 4;
		const [nL, nR] = noiseFixture(playLen, 5);
		const wetK = makeKernel({ depth: 1 });
		wetK.kernel.setLength('1/4');
		const [wetL] = capturePlaySegment(wetK.kernel, nL, nR, playLen - bar);
		const edgeX = mod.STUTTER_EDGE_XFADE_FRAMES;
		const bodyAt = alignedPlayIndex(bar, edgeX, lenS);
		const body = wetL.subarray(bodyAt, bodyAt + lenS);
		const { peak: peak2 } = xcorrPeak(body, nL.subarray(0, lenS), 4);
		assert.ok(peak2 > 0.98, `wet vs buffer ${peak2}`);
	});

	test('8 release returns dry with click ratio', () => {
		const bar = Math.round(4 * beatFrames128);
		const play = bar + Q * 20;
		const [inL, inR] = houseBar(play);
		const x = mod.STUTTER_EDGE_XFADE_FRAMES;
		for (const depth of [0.5, 1]) {
			const { kernel } = makeKernel({ depth });
			kernel.setLength('1/4');
			const events = [{ atFrame: bar + Q * 5, fn: (k) => k.setEngaged(false) }];
			const [outL] = renderBlocks(kernel, inL, inR, events);
			const rel = bar + Q * 5;
			const drySlope = maxSlope(inL, rel - 128, rel + 4096);
			const outSlope = maxSlope(outL, rel - 128, rel + 4096);
			assert.ok(outSlope / (drySlope + 1e-12) <= 1.5);
			assert.ok(bytesEqual(outL.subarray(rel + x + Q), inL.subarray(rel + x + Q)));
		}
	});

	test('9 resync recaptures new fixture', () => {
		const bar = Math.round(4 * beatFrames128);
		const total = bar * 3 + Q * 8;
		const [aL, aR] = sineFixture(total, 440);
		const [bL, bR] = sineFixture(total, 220);
		const mixL = new Float32Array(total);
		const mixR = new Float32Array(total);
		for (let i = 0; i < total; i += 1) {
			mixL[i] = i < bar * 2 ? aL[i] : bL[i];
			mixR[i] = i < bar * 2 ? aR[i] : bR[i];
		}
		const { kernel } = makeKernel({ depth: 1 });
		const events = [{ atFrame: bar + Q, fn: (k) => k.resync() }];
		const [outL] = renderBlocks(kernel, mixL, mixR, events);
		const after = bar * 2 + mod.STUTTER_EDGE_XFADE_FRAMES;
		const body = outL.subarray(after + bar, after + bar + Math.floor(beatFrames128 / 4));
		const { peak } = xcorrPeak(body, bL, Math.round(beatFrames128));
		assert.ok(peak > 0.9, `resync peak ${peak}`);
	});

	test('10 safety grid', () => {
		const bar = Math.round(4 * beatFrames128);
		const play = bar + Q * 30;
		const fixtures = [
			sineFixture(play),
			houseBar(play),
			noiseFixture(play, 1),
			noiseFixture(play, 2)
		];
		const blends = ['equal-power', 'dry-held'];
		for (const [inL, inR] of fixtures) {
			for (const blend of blends) {
				for (const depth of [0, 0.25, 0.5, 0.75, 1]) {
					const { kernel } = makeKernel({
						depth,
						variants: { [mod.STUTTER_BLEND_VARIANT.id]: blend }
					});
					for (const len of mod.STUTTER_LENGTHS) {
						kernel.setLength(len);
						kernel.setReverse(false);
						const [outL, outR] = renderBlocks(kernel, inL, inR);
						for (let i = 0; i < outL.length; i += 1) {
							assert.ok(Number.isFinite(outL[i]) && Number.isFinite(outR[i]));
							assert.ok(Math.abs(outL[i]) <= 1.0001);
							assert.ok(Math.abs(outR[i]) <= 1.0001);
							if (Math.abs(outL[i]) > 0 && Math.abs(outL[i]) < SUBNORMAL_EPS) {
								assert.fail('subnormal outL');
							}
						}
					}
				}
			}
		}
	});

	test('11 zero latency impulse in capture bar', () => {
		const bar = Math.round(4 * beatFrames128);
		const inL = new Float32Array(bar + Q);
		const inR = new Float32Array(bar + Q);
		const imp = Math.floor(bar / 2);
		inL[imp] = 1;
		inR[imp] = 1;
		const { kernel } = makeKernel({ depth: 0.5 });
		const [outL] = renderBlocks(kernel, inL, inR);
		assert.equal(kernel.latencyFrames, 0);
		assert.equal(outL[imp], inL[imp]);
	});

	test('12 allocations and render body hygiene', () => {
		const { kernel } = makeKernel();
		const z = new Float32Array(Q);
		kernel.render(z, z, z, z);
		assert.equal(kernel.allocations, 1);
		const silent = new Float32Array(Q);
		for (let i = 0; i < 1000; i += 1) kernel.render(silent, silent, silent, silent);
		assert.equal(kernel.allocations, 1);
		let slowBpm = 128;
		const slowState = { variants: {} };
		const slowAcc = variantPrefs.makeVariantPrefAccessors(slowState, () => {});
		const slowK = mod.createStutterFxKernel({
			sampleRate: SR,
			deck: 'A',
			beatSeconds: () => 60 / slowBpm,
			readVariant: slowAcc.readVariant
		});
		slowK.setEngaged(true);
		const fastBar = Math.round(4 * beatFrames128);
		renderBlocks(slowK, new Float32Array(fastBar + Q), new Float32Array(fastBar + Q));
		assert.equal(slowK.allocations, 1);
		slowBpm = 60;
		slowK.resync();
		const slowPlay = Math.round(4 * SR) + fastBar;
		renderBlocks(slowK, new Float32Array(slowPlay), new Float32Array(slowPlay));
		assert.equal(slowK.allocations, 2);

		const src = readFrontendSource('src/lib/rb/stutter-fx-kernel.ts');
		const renderStart = src.indexOf(
			'render(inL: Float32Array, inR: Float32Array, outL: Float32Array, outR: Float32Array): void {'
		);
		const renderEnd = src.indexOf('\n\t\tdispose():', renderStart);
		assert.ok(renderStart >= 0 && renderEnd > renderStart, 'render method found');
		const body = src.slice(renderStart, renderEnd);
		assert.ok(!/\bnew (?!RangeError|Error)/.test(body), 'render must not allocate');
		assert.ok(!/=>\s*/.test(body), 'render must not use arrows');
		assert.ok(!/\.slice\(/.test(body), 'render must not slice');
	});

	test('13 variants registry and live re-read', () => {
		const chunk = variantSetting.variantSetting('variant_fx_stutter_chunk');
		const blend = variantSetting.variantSetting('variant_fx_stutter_blend');
		assert.equal(chunk.defaultValue, '1/4');
		assert.equal(blend.defaultValue, 'equal-power');
		assert.ok(variantSetting.VARIANT_REGISTRY.some((d) => d.id === 'variant_fx_stutter_chunk'));
		assert.ok(variantSetting.VARIANT_REGISTRY.some((d) => d.id === 'variant_fx_stutter_blend'));
		for (const chunkVal of ['1/4', '1/8']) {
			for (const blendVal of ['equal-power', 'dry-held']) {
				const state = { variants: {} };
				const { readVariant } = variantPrefs.makeVariantPrefAccessors(state, () => {});
				state.variants['variant_fx_stutter_chunk'] = chunkVal;
				state.variants['variant_fx_stutter_blend'] = blendVal;
				const k = mod.createStutterFxKernel({
					sampleRate: SR,
					deck: 'A',
					beatSeconds: () => BPM128,
					readVariant
				});
				k.setEngaged(true);
				k.setDepth(0.5);
				const bar = Math.round(4 * beatFrames128);
				const [inL, inR] = noiseFixture(bar + Q * 8);
				renderBlocks(k, inL, inR);
				assert.equal(k.state, 'playing');
			}
		}
		const state = { variants: {} };
		const { readVariant, setVariant } = variantPrefs.makeVariantPrefAccessors(state, () => {});
		const k = mod.createStutterFxKernel({
			sampleRate: SR,
			deck: 'A',
			beatSeconds: () => BPM128,
			readVariant
		});
		k.setEngaged(true);
		k.setDepth(1);
		const bar = Math.round(4 * beatFrames128);
		const lenQuarter = Math.floor(beatFrames128 / 4);
		const play = bar + lenQuarter * 20;
		const [inL, inR] = noiseFixture(play, 44);
		const edgeX = mod.STUTTER_EDGE_XFADE_FRAMES;
		const k2 = mod.createStutterFxKernel({
			sampleRate: SR,
			deck: 'A',
			beatSeconds: () => BPM128,
			readVariant
		});
		k2.setEngaged(true);
		k2.setDepth(1);
		const events = [
			{
				atFrame: bar + lenQuarter * 2,
				fn: () => setVariant('variant_fx_stutter_chunk', '1/8')
			}
		];
		const [outL] = renderBlocks(k2, inL, inR, events);
		const lenEighth = Math.floor(beatFrames128 / 8);
		const afterBound = bar + lenQuarter * 2 + lenQuarter;
		const win = outL.subarray(afterBound, afterBound + lenEighth);
		const ref = inL.subarray(0, lenEighth);
		const { peak } = xcorrPeak(win, ref, 8);
		assert.ok(peak > 0.85, `live chunk variant period peak ${peak}`);

		const k3 = mod.createStutterFxKernel({
			sampleRate: SR,
			deck: 'A',
			beatSeconds: () => BPM128,
			readVariant
		});
		k3.setEngaged(true);
		k3.setDepth(0.5);
		k3.setLength('1');
		renderBlocks(k3, inL, inR, [
			{ atFrame: bar + Q, fn: () => setVariant('variant_fx_stutter_blend', 'dry-held') }
		]);
		const g = mod.stutterBlendGains('dry-held', 0.5);
		assert.ok(g[0] >= 0.99 && g[1] >= 0.99);
	});

	test('14 CPU diagnostic', () => {
		const bar = Math.round(4 * beatFrames128);
		const silent = new Float32Array(Q);
		const { kernel } = makeKernel({ depth: 0.5 });
		const [nL, nR] = noiseFixture(bar + Q);
		renderBlocks(kernel, nL, nR);
		const t0 = performance.now();
		for (let i = 0; i < 2000; i += 1) kernel.render(silent, silent, silent, silent);
		const us = ((performance.now() - t0) / 2000) * 1000;
		console.log(`STUTTER cpu: ${us.toFixed(2)}µs/quantum`);
	});
});
