// requirement: FIXT-12006
// [if] the same recipe renders different bytes on two calls, or the loader and direct imports differ [then] the module is not deterministic
// [if] a default recipe's sha256 differs from synth-signals.manifest.json [then] the fixture drifted; only --write-manifest moves it
// [if] a loop or click hit does not start at its grid beat, or downbeats are not every 4th beat from bar1_ms [then] the grid is not the one that placed the hits
// [if] a click downbeat is not accentDb above the other beats [then] the accent is broken
// [if] any default recipe peaks above -3 dBFS or holds a NaN, infinite or subnormal sample [then] the signal is unsafe to feed a kernel
// [if] 60 s of stereo 48 kHz loop takes 300 ms or more in node [then] the generator is too slow to share
// [if] a bad recipe field is accepted [then] validation is broken

import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { performance } from 'node:perf_hooks';
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { makeSignal as makeSignalDirect } from '../shared/synth-signals.ts';
import { loadTypeScriptModule } from './load-typescript.mjs';

const { makeSignal, signalBytes, DEFAULT_RECIPES, PEAK_LIMIT } = await loadTypeScriptModule(
	'tests/shared/synth-signals.ts'
);

const MANIFEST_URL = new URL('../shared/synth-signals.manifest.json', import.meta.url);
const FLT_MIN = 1.1754943508222875e-38;
const MIN_PEAK = Math.exp((-12 * Math.LN10) / 20);

const WRITE = process.argv.slice(2).includes('--write-manifest');
if (WRITE && process.env.NODE_TEST_CONTEXT) {
	throw new Error(
		'run the file directly: node --experimental-strip-types tests/unit/synth-signals.test.mjs --write-manifest'
	);
}

function sha256Bytes(bytes) {
	return createHash('sha256').update(bytes).digest('hex');
}

function hashRecipe(recipe) {
	return sha256Bytes(signalBytes(makeSignal(recipe)));
}

function buildManifest() {
	const recipes = {};
	for (const [name, recipe] of Object.entries(DEFAULT_RECIPES)) {
		const sig = makeSignal(recipe);
		const bytes = signalBytes(sig);
		recipes[name] = {
			recipe: { ...recipe },
			bytes: bytes.byteLength,
			sha256: sha256Bytes(bytes)
		};
	}
	return {
		schema_version: 1,
		why: 'sha256 of each default synth-signals recipe; moves only via synth-signals.test.mjs --write-manifest (issue #10402)',
		hashes:
			'sha256 hex of signalBytes(makeSignal(recipe)): left Float32 little-endian bytes, then right',
		recipes
	};
}

if (WRITE) {
	writeFileSync(MANIFEST_URL, `${JSON.stringify(buildManifest(), null, '\t')}\n`);
}

const manifest = JSON.parse(readFileSync(MANIFEST_URL, 'utf8'));

test('manifest matches live renders for all default recipes', () => {
	assert.equal(manifest.schema_version, 1);
	const names = Object.keys(DEFAULT_RECIPES);
	assert.deepEqual(Object.keys(manifest.recipes), names);
	assert.equal(names.length, 8);
	for (const name of names) {
		const entry = manifest.recipes[name];
		assert.deepEqual(entry.recipe, DEFAULT_RECIPES[name]);
		const bytes = signalBytes(makeSignal(DEFAULT_RECIPES[name]));
		assert.equal(entry.bytes, bytes.byteLength, name);
		const live = sha256Bytes(bytes);
		assert.equal(
			entry.sha256,
			live,
			`${name}: manifest sha256 drifted (got ${live}); rerun with --write-manifest only if the change is intended`
		);
	}
});

test('two calls and loader vs direct import are byte-identical', () => {
	for (const [name, recipe] of Object.entries(DEFAULT_RECIPES)) {
		const a = makeSignal(recipe);
		const b = makeSignal(recipe);
		assert.notEqual(a.left, b.left, name);
		assert.equal(sha256Bytes(signalBytes(a)), sha256Bytes(signalBytes(b)), name);
		const d = makeSignalDirect(recipe);
		assert.equal(sha256Bytes(signalBytes(makeSignal(recipe))), sha256Bytes(signalBytes(d)), name);
	}
});

test('peak and sample hygiene for defaults and structural recipes', () => {
	const extra = [
		{ kind: 'impulse', seconds: 1, sampleRate: 48000, seed: 10402, channels: 2 },
		{
			kind: 'oneshot_kick',
			seconds: 0.25,
			sampleRate: 48000,
			seed: 1,
			channels: 1
		},
		{
			kind: 'oneshot_snare',
			seconds: 0.25,
			sampleRate: 48000,
			seed: 1,
			channels: 1
		},
		{
			kind: 'oneshot_hat',
			seconds: 0.1,
			sampleRate: 48000,
			seed: 1,
			channels: 1
		}
	];
	const all = [...Object.values(DEFAULT_RECIPES), ...extra];
	for (const recipe of all) {
		const { left, right } = makeSignal(recipe);
		let peak = 0;
		const scan = (arr) => {
			for (let i = 0; i < arr.length; i++) {
				const x = arr[i];
				assert.ok(Number.isFinite(x), recipe.kind);
				assert.ok(x === 0 || Math.abs(x) >= FLT_MIN, recipe.kind);
				const a = Math.abs(x);
				if (a > peak) peak = a;
			}
		};
		scan(left);
		if (right !== left) scan(right);
		assert.ok(peak <= PEAK_LIMIT + 1e-7, recipe.kind);
		if (Object.values(DEFAULT_RECIPES).includes(recipe)) {
			assert.ok(peak >= MIN_PEAK, recipe.kind);
		}
	}
});

test('grid matches hit placement for loop and click defaults', () => {
	for (const name of ['loop_124_4bars', 'click_120_4bars']) {
		const recipe = DEFAULT_RECIPES[name];
		const { left, grid } = makeSignal(recipe);
		assert.ok(grid);
		assert.equal(grid.bpm, recipe.bpm);
		assert.equal(grid.beats_ms.length, 16);
		for (let i = 0; i < 16; i++) {
			assert.equal(grid.beats_ms[i], 100 + (i * 60000) / recipe.bpm);
		}
		assert.deepEqual(
			grid.downbeats_ms,
			grid.beats_ms.filter((_, i) => i % 4 === 0)
		);
		assert.equal(grid.bar1_ms, grid.beats_ms[0]);
		for (const ms of grid.beats_ms) {
			const idx = Math.round((ms * 48000) / 1000);
			let pre = 0;
			for (let j = Math.max(0, idx - 24); j < idx; j++) {
				pre = Math.max(pre, Math.abs(left[j]));
			}
			let win = 0;
			for (let j = idx; j < idx + 480 && j < left.length; j++) {
				win = Math.max(win, Math.abs(left[j]));
			}
			assert.ok(pre < 1e-4, `${name} beat ${ms}`);
			assert.ok(win > 0.1, `${name} beat ${ms}`);
		}
	}
	for (const name of ['white_2s', 'sweep_2s']) {
		const sig = makeSignal(DEFAULT_RECIPES[name]);
		assert.equal('grid' in sig, false);
	}
});

function windowPeak(left, idx, radius) {
	let p = 0;
	for (let j = Math.max(0, idx - radius); j <= Math.min(left.length - 1, idx + radius); j++) {
		p = Math.max(p, Math.abs(left[j]));
	}
	return p;
}

test('click accentDb separates downbeats from other beats', () => {
	const recipe = DEFAULT_RECIPES.click_120_4bars;
	const { left, grid } = makeSignal(recipe);
	const downPeaks = [];
	const upPeaks = [];
	const downSet = new Set(grid.downbeats_ms);
	for (const ms of grid.beats_ms) {
		const idx = Math.round((ms * 48000) / 1000);
		const p = windowPeak(left, idx, 240);
		if (downSet.has(ms)) downPeaks.push(p);
		else upPeaks.push(p);
	}
	const ratio = downPeaks[0] / upPeaks[0];
	const target = Math.exp((-recipe.accentDb * Math.LN10) / 20);
	const ratioDb = 20 * Math.log10(ratio);
	assert.ok(Math.abs(ratioDb - recipe.accentDb) <= 0.05);
	assert.ok(Math.abs(ratio - 1 / target) < 0.15);

	const flat = makeSignal({ ...recipe, accentDb: 0 });
	const flatGrid = flat.grid;
	const d0 = windowPeak(flat.left, Math.round((flatGrid.beats_ms[0] * 48000) / 1000), 240);
	const u0 = windowPeak(flat.left, Math.round((flatGrid.beats_ms[1] * 48000) / 1000), 240);
	assert.ok(Math.abs(d0 - u0) < 0.02);
});

test('impulse is a single sample at 10 ms', () => {
	const sig = makeSignal({
		kind: 'impulse',
		seconds: 1,
		sampleRate: 48000,
		seed: 10402,
		channels: 2
	});
	const expected = Math.fround(Math.exp((-6 * Math.LN10) / 20));
	let count = 0;
	let at = -1;
	for (let i = 0; i < sig.left.length; i++) {
		if (sig.left[i] !== 0) {
			count += 1;
			at = i;
		}
	}
	assert.equal(count, 1);
	assert.equal(at, 480);
	assert.equal(sig.left[480], expected);
});

test('mono shares buffers; stereo noise differs; stereo loop matches', () => {
	const mono = makeSignal({
		kind: 'white',
		seconds: 0.01,
		sampleRate: 48000,
		seed: 1,
		channels: 1
	});
	assert.equal(mono.left, mono.right);
	assert.equal(signalBytes(mono).byteLength, 4 * mono.left.length);
	const stereo = makeSignal({
		kind: 'white',
		seconds: 0.01,
		sampleRate: 48000,
		seed: 1,
		channels: 2
	});
	assert.notEqual(stereo.left, stereo.right);
	let same = true;
	for (let i = 0; i < stereo.left.length; i++) {
		if (stereo.left[i] !== stereo.right[i]) same = false;
	}
	assert.equal(same, false);
	const loop = makeSignal(DEFAULT_RECIPES.loop_124_4bars);
	for (let i = 0; i < loop.left.length; i++) {
		assert.equal(loop.left[i], loop.right[i]);
	}
});

test('different seeds differ; seed zero works', () => {
	const a = makeSignal({ ...DEFAULT_RECIPES.white_2s, seed: 1 });
	const b = makeSignal({ ...DEFAULT_RECIPES.white_2s, seed: 2 });
	assert.notEqual(sha256Bytes(signalBytes(a)), sha256Bytes(signalBytes(b)));
	const z = makeSignal({ ...DEFAULT_RECIPES.white_2s, seed: 0 });
	let any = false;
	for (let i = 0; i < z.left.length; i++) {
		if (z.left[i] !== 0) any = true;
	}
	assert.ok(any);
});

test('60 s stereo loop renders under 300 ms', () => {
	const recipe = {
		kind: 'loop',
		seconds: 60,
		sampleRate: 48000,
		seed: 10402,
		channels: 2,
		bpm: 124
	};
	makeSignal(recipe);
	const times = [];
	for (let i = 0; i < 3; i++) {
		const t0 = performance.now();
		const sig = makeSignal(recipe);
		times.push(performance.now() - t0);
		assert.equal(sig.left.length, 48000 * 60);
	}
	const best = Math.min(...times);
	assert.ok(best < 300, `loop 60s took ${best.toFixed(1)} ms`);
});

test('bad recipes throw RangeError', () => {
	const base = {
		kind: 'white',
		seconds: 1,
		sampleRate: 48000,
		seed: 1,
		channels: 2
	};
	const bad = [
		{ ...base, kind: 'nope' },
		{ ...base, seconds: 0 },
		{ ...base, seconds: Number.NaN },
		{ ...base, sampleRate: 44100.5 },
		{ ...base, channels: 3 },
		{ ...base, seed: -1 },
		{ ...base, seed: 1.5 },
		{ ...base, kind: 'click', bpm: 0, seconds: 2 },
		{ ...base, kind: 'loop', bars: 0, bpm: 120, seconds: 8 },
		{ ...base, kind: 'click', accentDb: -1, bpm: 120, seconds: 2 },
		{
			kind: 'click',
			seconds: 0.05,
			sampleRate: 48000,
			seed: 1,
			channels: 2,
			bpm: 120
		}
	];
	for (const recipe of bad) {
		assert.throws(() => makeSignal(recipe), RangeError);
	}
});
