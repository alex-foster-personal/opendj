// requirement: SNDCOL-05
// The renderer in this file is a TEST DOUBLE. It covers the pairing, blinding, manifest, veto and
// WAV plumbing (test coverage); it is not calibration evidence (AGENTS.md: a double is coverage,
// not feature evidence). Calibration renders only through the #9774 harness and #9803-#9805 kernels.
// [if] the manifest fails assertPairsManifestShape or has != 18 pairs [then] the sound color export is broken
// [if] two pairs share kernel, COLOR and variant set, or an id or clip path [then] a pair repeats
// [if] a clip is not 16-bit stereo PCM of the asked length, or a and b are identical [then] the WAV writer is broken
// [if] the same seed gives a different manifest, or sides never or always swap [then] seeded blinding is broken
// [if] a loudness delta is missing or NOISE deltas ignore gainMaxDb [then] ratings cannot map back to constants
// [if] a render over 0 dBFS or holding NaN still writes files [then] the hard vetoes are not gating the export
// [if] the CLI runs without --out and does not exit 1 with [ERROR] [then] the CLI does not fail fast
// [if] the real render path is missing and the CLI does not exit 2 with [UNAVAILABLE] or creates --out [then] calibration can run on a reimplementation
// [if] a non-real or missing renderer is accepted without allowTestDouble [then] the real-path gate is broken
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

const SCRIPT_URL = new URL('../../../../../scripts/export-sound-color-pairs.mjs', import.meta.url);
const mod = await import(SCRIPT_URL.href);
const {
	exportSoundColorPairs,
	assertPairsManifestShape,
	VARIANTS,
	KERNELS,
	PAIR_POSITIONS,
	PROXY_POSITIONS,
	VetoError,
	UnavailableError,
	DEFAULT_SEED,
	REAL_PATH_DEPENDENCIES,
	resolveRealRenderer,
	colorAtSample
} = mod;

const SR = 22050;
const DETENT = 0.02;
const dbToLin = (db) => 10 ** (db / 20);
const travel = (color) => {
	const a = Math.abs((color - 0.5) * 2);
	return a < DETENT ? 0 : (a - DETENT) / (1 - DETENT);
};
/** Deterministic, memoryless gain law per kernel; exactly 1 at the detent. */
const gainFor = (kernel, constants, color) => {
	const a = travel(color);
	if (a === 0) return 1;
	if (kernel === 'SWEEP') return 1 / (1 + constants.kMax * a);
	if (kernel === 'CRUSH') return 1 - constants.cwFixedAmount * a * 0.5;
	return 1;
};
const testDouble = (overrides = {}) => ({
	id: 'test-double:gain-law',
	realPath: false,
	async render({ kernel, constants, input, color, parameter, seed }) {
		return input.map((ch, c) => {
			let state = (seed ^ (0x4e00 + c)) >>> 0 || 1;
			return Float64Array.from(ch, (x, i) => {
				const col = colorAtSample(color, i);
				let y = x * gainFor(kernel, constants, col);
				if (kernel === 'NOISE' && travel(col) > 0) {
					state ^= state << 13;
					state ^= state >>> 17;
					state ^= state << 5;
					const z = (state >>> 0) / 0x100000000 - 0.5;
					y += 0.1 * dbToLin(constants.gainMaxDb) * parameter * travel(col) * z;
				}
				return y;
			});
		});
	},
	...overrides
});
const OPTS = { seconds: 1, sampleRate: SR, renderer: testDouble(), allowTestDouble: true };
const awayFromDetent = (color) => color.some(([, v]) => Math.abs(v - 0.5) > DETENT);
const dirs = [];
const freshDir = () => {
	const d = mkdtempSync(join(tmpdir(), 'sndcol-pairs-'));
	dirs.push(d);
	return d;
};
after(() => dirs.forEach((d) => rmSync(d, { recursive: true, force: true })));

let outDir;
let manifest;
before(async () => {
	outDir = freshDir();
	manifest = await exportSoundColorPairs({ outDir, ...OPTS });
});

const pairKey = (p) =>
	`${p.meta.kernel}|${p.meta.color}|${[p.meta.a_variant, p.meta.b_variant].sort().join(',')}`;

test('manifest has the #9776 shape with 18 pairs covering every kernel, variant pair and position', () => {
	assertPairsManifestShape(manifest);
	assert.equal(manifest.pairs.length, 18);
	assert.equal(manifest.seed, DEFAULT_SEED);
	assert.deepEqual(manifest.renderer, { id: 'test-double:gain-law', real_path: false });
	assert.equal(Object.keys(manifest.variants).length, Object.keys(VARIANTS).length);
	for (const kernel of KERNELS) {
		const ps = manifest.pairs.filter((p) => p.meta.kernel === kernel);
		assert.equal(ps.length, 6, kernel);
		for (const color of PAIR_POSITIONS)
			assert.equal(ps.filter((p) => p.meta.color === color).length, 3, `${kernel} ${color}`);
	}
	assert.deepEqual(
		new Set(manifest.pairs.map((p) => p.meta.fixture)),
		new Set(['house-vocal', 'drums'])
	);
	assert.deepEqual(JSON.parse(readFileSync(join(outDir, 'pairs.json'), 'utf8')), manifest);
});

test('no pair repeats', () => {
	const n = manifest.pairs.length;
	assert.equal(new Set(manifest.pairs.map(pairKey)).size, n);
	assert.equal(new Set(manifest.pairs.map((p) => p.id)).size, n);
	assert.equal(new Set(manifest.pairs.flatMap((p) => [p.a, p.b])).size, 2 * n);
	for (const p of manifest.pairs)
		assert.ok(!/baseline|gentle|hard/.test(p.a + p.b + p.question), `${p.id} leaks a variant name`);
});

test('every clip is a valid 1 s stereo 16-bit WAV and the two sides differ', () => {
	for (const p of manifest.pairs) {
		const [a, b] = [p.a, p.b].map((rel) => readFileSync(join(outDir, rel)));
		for (const wav of [a, b]) {
			assert.equal(wav.toString('ascii', 0, 4), 'RIFF');
			assert.equal(wav.toString('ascii', 8, 12), 'WAVE');
			assert.equal(wav.readUInt16LE(20), 1);
			assert.equal(wav.readUInt16LE(22), 2);
			assert.equal(wav.readUInt32LE(24), SR);
			assert.equal(wav.readUInt16LE(34), 16);
			assert.equal(wav.readUInt32LE(40), SR * 2 * 2);
		}
		assert.ok(!a.equals(b), `${p.id} a and b are byte-identical`);
	}
});

test('sides and order are seeded: same seed reproduces, a different seed changes them', async () => {
	const again = await exportSoundColorPairs({ outDir: freshDir(), ...OPTS });
	assert.deepEqual(again, manifest);
	const other = await exportSoundColorPairs({ outDir: freshDir(), ...OPTS, seed: 12345 });
	const layout = (m) => m.pairs.map((p) => `${pairKey(p)}:${p.meta.a_variant}`).join(';');
	assert.notEqual(layout(other), layout(manifest));
	const firstOnA = manifest.pairs.filter((p) => p.meta.a_variant < p.meta.b_variant).length;
	assert.ok(firstOnA > 0 && firstOnA < 18, `swap count ${firstOnA}`);
});

test('loudness proxy is recorded for both variants at all four positions', () => {
	for (const p of manifest.pairs) {
		for (const name of [p.meta.a_variant, p.meta.b_variant]) {
			assert.deepEqual(Object.keys(p.meta.loudness_delta_db[name]), PROXY_POSITIONS.map(String));
			if (p.meta.kernel === 'SWEEP')
				assert.ok(p.meta.loudness_delta_db[name]['0.85'] < 0, `${p.id} SWEEP CW not below dry`);
		}
		assert.equal(
			p.meta.metric,
			p.meta.kernel === 'NOISE' ? 'noise_to_programme_db' : 'rms_db_vs_dry'
		);
	}
	const noise = {};
	for (const p of manifest.pairs.filter((q) => q.meta.kernel === 'NOISE'))
		Object.assign(noise, p.meta.loudness_delta_db);
	assert.ok(
		noise.gentle['0.85'] < noise.baseline['0.85'] && noise.baseline['0.85'] < noise.hard['0.85']
	);
});

const overrideSweep = (fn) =>
	testDouble({
		async render(req) {
			const out = await testDouble().render(req);
			return req.kernel === 'SWEEP' && awayFromDetent(req.color) ? out.map(fn) : out;
		}
	});

test('a peak veto stops the export and writes nothing', async () => {
	const dir = freshDir();
	await assert.rejects(
		exportSoundColorPairs({
			outDir: dir,
			...OPTS,
			renderer: overrideSweep((ch) => ch.map((x) => x * 4 + 1))
		}),
		(err) => {
			assert.ok(err instanceof VetoError);
			assert.equal(err.veto, 'peak');
			assert.match(err.message, /kernel=SWEEP/);
			return true;
		}
	);
	assert.deepEqual(readdirSync(dir), []);
});

test('a finite veto stops the export and writes nothing', async () => {
	const dir = freshDir();
	await assert.rejects(
		exportSoundColorPairs({
			outDir: dir,
			...OPTS,
			renderer: overrideSweep((ch) => ch.map(() => Number.NaN))
		}),
		(err) => {
			assert.ok(err instanceof VetoError);
			assert.equal(err.veto, 'finite');
			return true;
		}
	);
	assert.ok(!existsSync(join(dir, 'pairs.json')) && !existsSync(join(dir, 'clips')));
});

test('the CLI fails fast without --out', () => {
	const res = spawnSync(process.execPath, [fileURLToPath(SCRIPT_URL)], { encoding: 'utf8' });
	assert.equal(res.status, 1);
	assert.match(res.stderr, /^\[ERROR\].*--out/);
});

/** Issues still blocking calibration (kernel files on disk vs harness adapter wired). */
function unresolvedCalibrationIssues() {
	try {
		resolveRealRenderer();
		return [];
	} catch (err) {
		assert.ok(err instanceof UnavailableError);
		return err.missing.map((d) => d.issue);
	}
}

// Holds until the #9774 harness adapter is wired in resolveRealRenderer; #9804 may be on-disk
// before the export path can call it.
test('the CLI reports UNAVAILABLE and writes nothing while the real render path is missing', () => {
	const out = join(freshDir(), 'round1');
	const res = spawnSync(process.execPath, [fileURLToPath(SCRIPT_URL), '--out', out], {
		encoding: 'utf8'
	});
	assert.equal(res.status, 2);
	assert.match(res.stderr, /^\[UNAVAILABLE\]/);
	const issues = unresolvedCalibrationIssues();
	if (issues.length > 0) {
		for (const issue of issues) assert.ok(res.stderr.includes(issue), issue);
	} else {
		assert.match(res.stderr, /no adapter|#9774/);
	}
	assert.equal(res.stderr.includes('#9805'), false, 'CRUSH is on main');
	assert.equal(existsSync(out), false);
});

test('resolveRealRenderer names every missing dependency', () => {
	const repoRoot = fileURLToPath(new URL('../../../../../', import.meta.url));
	const expectedMissing = REAL_PATH_DEPENDENCIES.filter(
		(d) => !existsSync(join(repoRoot, d.path))
	).map((d) => d.issue);
	assert.throws(
		() => resolveRealRenderer(repoRoot),
		(err) => {
			assert.ok(err instanceof UnavailableError);
			assert.deepEqual(
				err.missing.map((d) => d.issue),
				expectedMissing
			);
			return true;
		}
	);
});

test('resolveRealRenderer stays UNAVAILABLE without an adapter even when every dependency file exists', () => {
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

test('a non-real renderer is refused without allowTestDouble', async () => {
	const dir = freshDir();
	await assert.rejects(
		exportSoundColorPairs({ outDir: dir, seconds: 1, sampleRate: SR, renderer: testDouble() }),
		/not a real-path renderer/
	);
	assert.deepEqual(readdirSync(dir), []);
});

test('a missing renderer is refused', async () => {
	await assert.rejects(
		exportSoundColorPairs({ outDir: freshDir(), seconds: 1, sampleRate: SR }),
		/renderer/
	);
});
