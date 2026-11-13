// requirement: FXMOD-9838
// The renderer in this file is a TEST DOUBLE (pipeline coverage only, not calibration evidence).
// [if] canonicalPairs length is not 24 [then] the voicing pair design is wrong
// [if] the manifest fails assertPairsManifestShape [then] the mod FX export is broken
// [if] visible pair fields leak kernel, axis or candidate values [then] blinding is broken
// [if] a vetoed render still writes files [then] the hard vetoes are not gating the export
// [if] the CLI runs without the real path and does not exit 2 [then] calibration can run on a reimplementation
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import {
	existsSync,
	mkdirSync,
	mkdtempSync,
	readFileSync,
	readdirSync,
	rmSync,
	writeFileSync
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';

const SCRIPT_URL = new URL(
	'../../../../../scripts/export-mod-fx-rating-pairs.mjs',
	import.meta.url
);
const mod = await import(SCRIPT_URL.href);
const {
	exportModFxRatingPairs,
	assertPairsManifestShape,
	canonicalPairs,
	AXES,
	FIXTURES,
	BASELINE,
	KERNELS,
	PAIR_DEPTH,
	PROXY_DEPTHS,
	VetoError,
	UnavailableError,
	DEFAULT_SEED,
	REAL_PATH_DEPENDENCIES,
	resolveRealRenderer,
	monoFoldLossDb,
	integratedLufs,
	resonancePeakDb
} = mod;

const SR = 22050;

/** Pipeline-only static delay mix; not a flanger or phaser kernel. */
const baseDouble = () => ({
	id: 'test-double:static-mix',
	realPath: false,
	async render({ voicing, depth, input }) {
		const m =
			voicing.depthCurve === 'equal-power' ? 0.5 * Math.sin((depth * Math.PI) / 2) : 0.5 * depth;
		const feedback = voicing.feedback ?? 0;
		const stages = voicing.stages ?? 0;
		const spreadDeg = voicing.spreadDeg ?? 0;
		const d = 8 + Math.round(feedback * 10) + stages;
		const dR = d + Math.round(spreadDeg / 15);
		return input.map((ch, c) => {
			const delay = c === 0 ? d : dR;
			return Float64Array.from(
				ch,
				(x, n) => 0.9 * ((1 - m) * x + (n >= delay ? m * ch[n - delay] : 0))
			);
		});
	}
});

const testDouble = (overrides = {}) => ({
	...baseDouble(),
	...overrides,
	render: overrides.render ?? baseDouble().render
});

const OPTS = { bars: 1, sampleRate: SR, renderer: testDouble(), allowTestDouble: true };
const dirs = [];
const freshDir = () => {
	const d = mkdtempSync(join(tmpdir(), 'mod-fx-pairs-'));
	dirs.push(d);
	return d;
};
after(() => dirs.forEach((d) => rmSync(d, { recursive: true, force: true })));

let outDir;
let manifest;

before(async () => {
	outDir = freshDir();
	manifest = await exportModFxRatingPairs({ outDir, ...OPTS });
});

const canonicalKey = (e) => `${e.kernel}|${e.axis}|${[e.x, e.y].sort().join()}|${e.fixture}`;

test('canonicalPairs design: 24 entries, axes and fixtures', () => {
	const c = canonicalPairs(FIXTURES);
	assert.equal(c.length, 24);
	for (const ax of AXES) {
		const subset = c.filter((e) => e.kernel === ax.kernel && e.axis === ax.axis);
		if (ax.values.length === 3) {
			assert.equal(subset.length, 3);
			assert.deepEqual(new Set(subset.map((e) => e.fixture)), new Set(FIXTURES.map((f) => f.name)));
		} else {
			assert.equal(subset.length, 3);
		}
	}
	assert.equal(new Set(c.map(canonicalKey)).size, 24);
	for (const e of c) {
		const va = { ...BASELINE[e.kernel], [e.axis]: e.x };
		const vb = { ...BASELINE[e.kernel], [e.axis]: e.y };
		let diff = 0;
		for (const k of Object.keys(va)) if (va[k] !== vb[k]) diff++;
		assert.equal(diff, 1, `${e.kernel} ${e.axis}`);
	}
});

test('export writes 24 pairs, 48 WAVs and a valid manifest', () => {
	assertPairsManifestShape(manifest);
	assert.equal(manifest.pairs.length, 24);
	assert.equal(manifest.seed, DEFAULT_SEED);
	assert.deepEqual(manifest.renderer, { id: 'test-double:static-mix', real_path: false });
	assert.ok(manifest.hidden_fields.includes('meta'));
	const { _render_call_count: _rc, ...onDisk } = manifest;
	assert.deepEqual(JSON.parse(readFileSync(join(outDir, 'pairs.json'), 'utf8')), onDisk);
	for (const p of manifest.pairs) {
		for (const rel of [p.a, p.b]) {
			const wav = readFileSync(join(outDir, rel));
			assert.equal(wav.readUInt32LE(24), SR);
			assert.equal(wav.readUInt16LE(22), 2);
			assert.equal(wav.readUInt16LE(34), 16);
		}
	}
});

test('blindness: clip paths and ids leak no kernel, axis or candidate', () => {
	const banned =
		/flanger|phaser|feedback|spreadDeg|cycleBeats|depthCurve|stages|0\.3|0\.55|0\.75|equal-power/;
	for (const p of manifest.pairs) {
		const vis = `${p.id} ${p.a} ${p.b}`;
		assert.ok(!banned.test(vis), p.id);
	}
	assert.ok(manifest.hidden_fields.includes('meta'));
	for (const p of manifest.pairs) {
		for (const side of ['a', 'b']) {
			const px = p.meta.proxies[side];
			assert.ok(Number.isFinite(px.resonance_peak_db));
			assert.ok(Number.isFinite(px.median_notch_depth_db));
		}
	}
});

test('seeded order and side swap', async () => {
	const againDir = freshDir();
	const again = await exportModFxRatingPairs({ outDir: againDir, ...OPTS });
	const onDisk = (m, dir) => {
		const { _render_call_count: _rc, ...rest } = m;
		assert.deepEqual(JSON.parse(readFileSync(join(dir, 'pairs.json'), 'utf8')), rest);
		return rest;
	};
	assert.deepEqual(onDisk(again, againDir), onDisk(manifest, outDir));
	const other = await exportModFxRatingPairs({ outDir: freshDir(), ...OPTS, seed: 777 });
	const layout = (m) => m.pairs.map((p) => `${p.meta.kernel}:${p.meta.a_candidate}`).join(';');
	assert.notEqual(layout(other), layout(manifest));
	const sideSwapped = (p) => {
		const ax = AXES.find((a) => a.kernel === p.meta.kernel && a.axis === p.meta.axis);
		return ax.values.indexOf(p.meta.a_candidate) > ax.values.indexOf(p.meta.b_candidate);
	};
	const swapped = manifest.pairs.filter(sideSwapped);
	const unswapped = manifest.pairs.filter((p) => !sideSwapped(p));
	assert.ok(swapped.length > 0);
	assert.ok(unswapped.length > 0);
});

test('render cache keeps calls below naive 24*2*4', async () => {
	let calls = 0;
	const counted = testDouble({
		async render(req) {
			calls++;
			return testDouble().render(req);
		}
	});
	const m = await exportModFxRatingPairs({ outDir: freshDir(), ...OPTS, renderer: counted });
	assert.ok(calls < 24 * 2 * 4);
	assert.equal(m.pairs.length, 24);
});

const overrideRender = (mapFn) =>
	testDouble({
		async render(req) {
			const out = await testDouble().render(req);
			return mapFn(out, req);
		}
	});

test('peak veto writes nothing', async () => {
	const dir = freshDir();
	await assert.rejects(
		exportModFxRatingPairs({
			outDir: dir,
			...OPTS,
			renderer: overrideRender((ch) => ch.map((c) => c.map((x) => x * 4 + 1.5)))
		}),
		(err) => {
			assert.ok(err instanceof VetoError);
			assert.equal(err.veto, 'peak');
			assert.match(err.file, /^clips\/p\d\d-[ab]\.wav$|^proxy:/);
			return true;
		}
	);
	assert.ok(!existsSync(dir) || readdirSync(dir).length === 0);
});

test('finite veto writes nothing', async () => {
	const dir = freshDir();
	await assert.rejects(
		exportModFxRatingPairs({
			outDir: dir,
			...OPTS,
			renderer: overrideRender((ch) => ch.map((c) => c.map(() => Number.NaN)))
		}),
		(err) => {
			assert.ok(err instanceof VetoError);
			assert.equal(err.veto, 'finite');
			return true;
		}
	);
	assert.ok(!existsSync(join(dir, 'pairs.json')));
});

test('mono-fold veto writes nothing', async () => {
	const dir = freshDir();
	await assert.rejects(
		exportModFxRatingPairs({
			outDir: dir,
			...OPTS,
			renderer: overrideRender((ch) => [ch[0], Float64Array.from(ch[0], (x) => -x)])
		}),
		(err) => {
			assert.ok(err instanceof VetoError);
			assert.equal(err.veto, 'mono-fold');
			return true;
		}
	);
	assert.ok(!existsSync(join(dir, 'clips')));
});

test('non-real renderer refused without allowTestDouble', async () => {
	const dir = freshDir();
	await assert.rejects(
		exportModFxRatingPairs({ outDir: dir, bars: 1, sampleRate: SR, renderer: testDouble() }),
		/not a real-path/
	);
});

test('missing renderer and non-empty outDir refused', async () => {
	await assert.rejects(
		exportModFxRatingPairs({ outDir: freshDir(), bars: 1, sampleRate: SR }),
		/renderer/
	);
	const dir = freshDir();
	writeFileSync(join(dir, 'x'), 'x');
	await assert.rejects(exportModFxRatingPairs({ outDir: dir, ...OPTS }), /not empty/);
});

test('monoFoldLossDb and integratedLufs probes', () => {
	const n = 4096;
	const l = new Float64Array(n).fill(0.1);
	assert.equal(monoFoldLossDb([l, l]), 0);
	const r = new Float64Array(n);
	let s = 42;
	const noise = () => {
		s ^= s << 13;
		s ^= s >>> 17;
		s ^= s << 5;
		return (s >>> 0) / 0x100000000 - 0.5;
	};
	for (let i = 0; i < n; i++) {
		l[i] = noise();
		r[i] = noise();
	}
	const fold = monoFoldLossDb([l, r]);
	assert.ok(Math.abs(fold - 3.01) < 0.2, fold);
	const anti = Float64Array.from(l, (x) => -x);
	assert.equal(monoFoldLossDb([l, anti]), Number.POSITIVE_INFINITY);
	const t = Math.round(SR / 1000);
	const sine = Float64Array.from(
		{ length: SR },
		(_, i) => 0.1 * Math.sin((2 * Math.PI * 1000 * i) / SR)
	);
	const lufs = integratedLufs([sine, sine], SR);
	assert.ok(Number.isFinite(lufs));
	assert.ok(Math.abs(lufs - -20.0) < 0.3, `integratedLufs sine probe: ${lufs}`);
	const dry = [new Float64Array(t), new Float64Array(t)];
	assert.equal(resonancePeakDb(dry, dry, SR), 0);
});

test('CLI without --out exits 1', () => {
	const res = spawnSync(process.execPath, [fileURLToPath(SCRIPT_URL)], { encoding: 'utf8' });
	assert.equal(res.status, 1);
	assert.match(res.stderr, /^\[ERROR\].*--out/);
});

// Holds only while #9774 and #9776 are absent on main (#9834, #9835 landed the mod FX kernels); rewrite when an adapter is wired.
test('CLI reports UNAVAILABLE and writes nothing', () => {
	const out = join(freshDir(), 'round1');
	const res = spawnSync(process.execPath, [fileURLToPath(SCRIPT_URL), '--out', out], {
		encoding: 'utf8'
	});
	assert.equal(res.status, 2);
	assert.match(res.stderr, /^\[UNAVAILABLE\]/);
	for (const issue of ['#9774', '#9776']) assert.ok(res.stderr.includes(issue), issue);
	assert.equal(existsSync(out), false);
});

test('resolveRealRenderer lists missing deps then no adapter', () => {
	assert.throws(
		() => resolveRealRenderer(freshDir()),
		(err) => {
			assert.ok(err instanceof UnavailableError);
			assert.deepEqual(
				err.missing.map((d) => d.issue),
				['#9774', '#9834', '#9835', '#9776']
			);
			return true;
		}
	);
	const root = freshDir();
	for (const d of REAL_PATH_DEPENDENCIES) {
		mkdirSync(dirname(join(root, d.path)), { recursive: true });
		writeFileSync(join(root, d.path), '');
	}
	assert.throws(
		() => resolveRealRenderer(root),
		(err) => {
			assert.ok(err instanceof UnavailableError);
			assert.deepEqual(err.missing, []);
			assert.match(err.message, /no adapter/);
			return true;
		}
	);
});
