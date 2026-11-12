// requirement: none (test infrastructure, issue #9845)
// The renderer in this file is a TEST DOUBLE. It covers the pairing, blinding, measures, loudness
// match, veto and WAV plumbing (test coverage); it is not calibration evidence. Calibration renders
// only through the #9774 harness, the #9793 ROLL kernel and the #9840 deck roll.
// [if] the manifest fails assertRollPairsManifestShape or is not 8 pairs / 16 trials anchored on ANCHOR [then] the roll export is broken
// [if] a pair's two trials are not mirrored [then] side swapping is broken
// [if] a variant id leaks into a clip path or the question [then] blinding is broken
// [if] a clip is not 16-bit stereo PCM of 8 bars, or the two clips of a pair match [then] the WAV writer is broken
// [if] a clip RMS is off TARGET_RMS_DBFS by more than 0.1 dB [then] the loudness match is broken
// [if] a row lacks fixture, knob, variants or a finite measure, or the measures ignore the law or edge [then] ratings cannot map back to constants
// [if] the same seed gives different bytes, or sides never or always put the anchor on a [then] seeded blinding is broken
// [if] a clip over 0 dBFS still writes files or the error omits the file [then] the peak veto is not gating the export
// [if] a NaN render still writes files [then] the finite veto is not gating the export
// [if] a non-real renderer, no renderer or a bad defaultKnob is accepted [then] the renderer gate is broken
// [if] the CLI runs without --out and does not exit 1 with [ERROR] [then] the CLI does not fail fast
// [if] the CLI does not exit 2 with [UNAVAILABLE] or creates --out [then] calibration can run on a reimplementation
// [if] resolveRealRenderer does not name #9774, #9793 and #9840 [then] the real-path gate is broken
// [if] main() hits a veto and does not return 1 with an [ERROR] line naming the file [then] the CLI veto path is broken
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

const SCRIPT_URL = new URL('../../scripts/export-roll-rating-pairs.mjs', import.meta.url);
const {
	exportRollRatingPairs,
	assertRollPairsManifestShape,
	resolveRealRenderer,
	main,
	VARIANTS,
	ANCHOR,
	SHORT_ROLL_BEATS,
	TARGET_RMS_DBFS,
	QUESTION,
	VetoError,
	UnavailableError,
	REAL_PATH_DEPENDENCIES
} = await import(SCRIPT_URL.href);

// 8 kHz keeps every assertion while cutting CPU so timing-sensitive tests sharing the runner are not starved.
const SR = 8000;
const FRAMES = Math.round(((8 * 4 * 60) / 124) * SR);
const DEFAULT_KNOB = 0.75;

//----------------------------------------------------------------------------- test double
const _gains = (c, k) => {
	if (c.law === 'equal-gain') return [1 - k, k];
	if (c.law === 'equal-power') return [Math.cos((k * Math.PI) / 2), Math.sin((k * Math.PI) / 2)];
	if (c.law === 'hold') {
		const wet = 10 ** (c.holdDb / 20);
		if (k <= c.holdUntil) return [1, wet];
		const t = (k - c.holdUntil) / (1 - c.holdUntil);
		return [1 - t, wet + (1 - wet) * t];
	}
	throw new Error(`unknown law ${c.law}`);
};
const testDouble = (fault = () => {}) => ({
	id: 'test-double:roll',
	realPath: false,
	defaultKnob: DEFAULT_KNOB,
	async render(req) {
		const { feature, constants, input, sampleRate, bpm, knob, rollBeats, onSample, offSample } =
			req;
		const len = Math.round(((rollBeats * 60) / bpm) * sampleRate);
		const edgeMs = feature === 'seam-edge' ? constants.edgeMs : 1;
		const edge = Math.round((edgeMs * sampleRate) / 1000);
		const [dryG, wetG] = feature === 'level-law' ? _gains(constants, knob) : [0, 1];
		const fade = (j) => (edge === 0 ? 1 : Math.min(1, (j + 1) / edge, (len - j) / edge));
		const out = input.map((ch) => {
			const o = Float64Array.from(ch);
			for (let i = Math.max(0, onSample); i < Math.min(ch.length, offSample); i++) {
				const j = (i - onSample) % len;
				o[i] = dryG * ch[i] + wetG * ch[onSample + j] * fade(j);
			}
			return o;
		});
		const seams = [];
		for (let s = onSample + len; s < offSample; s += len) seams.push(s);
		fault(req, out);
		return { out, seams };
	}
});
const spikeHard = (req, out) => {
	if (req.variant === 'hard') out[0][req.onSample + 10] = 50;
};
const nanOnce = (req, out) => {
	if (req.variant === 'edge-3ms') out[1][5] = NaN;
};

//----------------------------------------------------------------------------- helpers
const dirs = [];
const freshDir = () => {
	const d = mkdtempSync(join(tmpdir(), 'roll-pairs-'));
	dirs.push(d);
	return d;
};
const run = (opts = {}) =>
	exportRollRatingPairs({
		outDir: freshDir(),
		renderer: testDouble(),
		allowTestDouble: true,
		sampleRate: SR,
		...opts
	});
const readClip = (dir, rel) => {
	const buf = readFileSync(join(dir, rel));
	const frames = buf.readUInt32LE(40) / 4;
	const ch = [new Float64Array(frames), new Float64Array(frames)];
	for (let i = 0; i < frames; i++)
		for (let c = 0; c < 2; c++) ch[c][i] = buf.readInt16LE(44 + i * 4 + c * 2) / 32767;
	return { buf, ch, frames };
};
const rmsDb = (ch) => {
	let s = 0;
	for (const c of ch) for (const x of c) s += x * x;
	return 20 * Math.log10(Math.sqrt(s / (ch.length * ch[0].length)));
};
const byPair = (m) => {
	const g = new Map();
	for (const t of m.pairs) g.set(t.meta.pair_id, [...(g.get(t.meta.pair_id) ?? []), t]);
	return g;
};

let outDir;
let manifest;
before(async () => {
	outDir = freshDir();
	manifest = await exportRollRatingPairs({
		outDir,
		renderer: testDouble(),
		allowTestDouble: true,
		sampleRate: SR
	});
});
after(() => {
	for (const d of dirs) rmSync(d, { recursive: true, force: true });
});

//----------------------------------------------------------------------------- tests
test('manifest shape: 8 anchored pairs, 16 trials, on disk equals returned', () => {
	assertRollPairsManifestShape(manifest);
	assert.equal(manifest.pairs.length, 16);
	const pairs = [...byPair(manifest).values()].map((ts) => ts[0].meta);
	assert.equal(pairs.length, 8);
	const level = pairs.filter((m) => m.feature === 'level-law');
	const seam = pairs.filter((m) => m.feature === 'seam-edge');
	assert.deepEqual(level.map((m) => m.knob).sort(), [0.5, 0.5, DEFAULT_KNOB, DEFAULT_KNOB]);
	assert.deepEqual(
		seam.map((m) => m.roll_beats).sort(),
		[...SHORT_ROLL_BEATS, ...SHORT_ROLL_BEATS].sort()
	);
	for (const m of pairs)
		assert.ok(
			[m.a_variant, m.b_variant].includes(ANCHOR[m.feature]),
			`pair ${m.pair_id} lacks the anchor`
		);
	assert.deepEqual(JSON.parse(readFileSync(join(outDir, 'pairs.json'), 'utf8')), manifest);
});

test('each pair appears in two mirrored trials', () => {
	for (const [pid, [t1, t2]] of byPair(manifest)) {
		assert.equal(t1.a, t2.b, pid);
		assert.equal(t1.b, t2.a, pid);
		assert.equal(t1.meta.a_variant, t2.meta.b_variant, pid);
		assert.equal(t1.meta.b_variant, t2.meta.a_variant, pid);
	}
});

test('no variant id leaks into a clip path or the question', () => {
	const ids = Object.values(VARIANTS).flatMap((v) => Object.keys(v));
	for (const t of manifest.pairs) {
		for (const id of ids) {
			assert.ok(
				!t.a.includes(id) && !t.b.includes(id) && !t.question.includes(id),
				`${id} leaks in ${t.id}`
			);
		}
		assert.equal(t.question, QUESTION);
	}
});

test('16 clips: 16-bit stereo PCM, 8 bars long, pair sides differ, loudness matched', () => {
	const files = readdirSync(join(outDir, 'clips')).sort();
	assert.equal(files.length, 16);
	for (const f of files) {
		const { buf, ch, frames } = readClip(outDir, `clips/${f}`);
		assert.equal(buf.toString('ascii', 0, 4), 'RIFF');
		assert.equal(buf.toString('ascii', 8, 12), 'WAVE');
		assert.equal(buf.readUInt16LE(22), 2);
		assert.equal(buf.readUInt32LE(24), SR);
		assert.equal(buf.readUInt16LE(34), 16);
		assert.equal(frames, FRAMES);
		assert.ok(Math.abs(rmsDb(ch) - TARGET_RMS_DBFS) < 0.1, `${f} rms ${rmsDb(ch)}`);
	}
	for (const [pid, [t]] of byPair(manifest)) {
		assert.ok(
			!readClip(outDir, t.a).buf.equals(readClip(outDir, t.b).buf),
			`${pid} sides identical`
		);
	}
});

test('meta names fixture, knob, roll, variants and finite measures that track the law and edge', () => {
	for (const t of manifest.pairs) {
		const m = t.meta;
		assert.equal(m.fixture, 'house-124');
		assert.equal(typeof m.knob, 'number');
		assert.equal(typeof m.roll_beats, 'number');
		for (const key of ['wet_dry_db', 'seam_click_ratio', 'match_gain_db']) {
			for (const v of [m.a_variant, m.b_variant])
				assert.ok(Number.isFinite(m[key][v]), `${t.id} ${key}.${v}`);
		}
	}
	const find = (pred) => manifest.pairs.find((t) => pred(t.meta)).meta;
	const mid = find((m) => m.knob === 0.5 && [m.a_variant, m.b_variant].includes('equal-gain'));
	assert.ok(
		mid.wet_dry_db['equal-gain'] < mid.wet_dry_db['equal-power'],
		'linear law should dip at mid knob'
	);
	for (const rb of SHORT_ROLL_BEATS) {
		const hard = find((m) => m.roll_beats === rb && [m.a_variant, m.b_variant].includes('hard'))
			.seam_click_ratio.hard;
		const soft = find((m) => m.roll_beats === rb && [m.a_variant, m.b_variant].includes('edge-3ms'))
			.seam_click_ratio['edge-3ms'];
		assert.ok(hard > soft, `roll ${rb}: hard ${hard} <= edge-3ms ${soft}`);
	}
});

test('same seed is byte-identical; a different seed changes the layout; sides vary', async () => {
	const dir2 = freshDir();
	await exportRollRatingPairs({
		outDir: dir2,
		renderer: testDouble(),
		allowTestDouble: true,
		sampleRate: SR
	});
	assert.ok(
		readFileSync(join(outDir, 'pairs.json')).equals(readFileSync(join(dir2, 'pairs.json')))
	);
	for (const f of readdirSync(join(outDir, 'clips'))) {
		assert.ok(readClip(outDir, `clips/${f}`).buf.equals(readClip(dir2, `clips/${f}`).buf), f);
	}
	const other = await run({ seed: 12345 });
	assert.notDeepEqual(
		other.pairs.map((t) => [t.a, t.b, t.meta.a_variant]),
		manifest.pairs.map((t) => [t.a, t.b, t.meta.a_variant])
	);
	const anchorOnA = [...byPair(manifest).values()]
		.map(([t1, t2]) => (t1.meta.trial === 1 ? t1 : t2))
		.map((t) => t.meta.a_variant === ANCHOR[t.meta.feature]);
	assert.ok(anchorOnA.includes(true) && anchorOnA.includes(false));
});

test('peak veto names the file and writes nothing', async () => {
	const dir = freshDir();
	await assert.rejects(
		exportRollRatingPairs({
			outDir: dir,
			renderer: testDouble(spikeHard),
			allowTestDouble: true,
			sampleRate: SR
		}),
		(err) =>
			err instanceof VetoError &&
			err.veto === 'peak' &&
			/file=clips\/p0\d-[12]\.wav/.test(err.message)
	);
	assert.deepEqual(readdirSync(dir), []);
});

test('finite veto names the file and writes nothing', async () => {
	const dir = freshDir();
	await assert.rejects(
		exportRollRatingPairs({
			outDir: dir,
			renderer: testDouble(nanOnce),
			allowTestDouble: true,
			sampleRate: SR
		}),
		(err) =>
			err instanceof VetoError &&
			err.veto === 'finite' &&
			/file=clips\/p0\d-[12]\.wav/.test(err.message)
	);
	assert.deepEqual(readdirSync(dir), []);
});

test('renderer gate: non-real, missing and bad defaultKnob are refused', async () => {
	await assert.rejects(
		exportRollRatingPairs({ outDir: freshDir(), renderer: testDouble(), sampleRate: SR }),
		/not a real-path renderer/
	);
	await assert.rejects(exportRollRatingPairs({ outDir: freshDir(), sampleRate: SR }), /renderer/);
	for (const knob of [0.5, 0, 1, NaN]) {
		await assert.rejects(
			run({ renderer: { ...testDouble(), defaultKnob: knob } }),
			/defaultKnob/,
			`knob ${knob}`
		);
	}
});

test('CLI without --out exits 1 with [ERROR]', () => {
	const r = spawnSync(process.execPath, [fileURLToPath(SCRIPT_URL)], { encoding: 'utf8' });
	assert.equal(r.status, 1);
	assert.match(r.stderr, /^\[ERROR\].*--out/);
});

// Names exactly the real render path dependencies still absent from the repo (#9793 landed the ROLL kernel).
test('CLI with --out exits 2 with [UNAVAILABLE] and creates nothing', () => {
	const repoRoot = fileURLToPath(new URL('../../../../../', import.meta.url));
	assert.ok(existsSync(join(repoRoot, 'apps/webui/frontend')));
	const missing = REAL_PATH_DEPENDENCIES.filter((d) => !existsSync(join(repoRoot, d.path)));
	const out = join(freshDir(), 'not-created');
	const r = spawnSync(process.execPath, [fileURLToPath(SCRIPT_URL), '--out', out], {
		encoding: 'utf8'
	});
	assert.equal(r.status, 2);
	assert.match(r.stderr, /^\[UNAVAILABLE\]/);
	assert.equal(existsSync(out), false);
	assert.ok(!missing.some((d) => d.issue === '#9793'));
	for (const dep of REAL_PATH_DEPENDENCIES) {
		if (missing.some((d) => d.issue === dep.issue)) {
			assert.ok(r.stderr.includes(dep.issue), dep.issue);
		} else {
			assert.ok(!r.stderr.includes(dep.issue), `must not name present ${dep.issue}`);
		}
	}
});

test('resolveRealRenderer names each missing dependency, then has no adapter', () => {
	const root = freshDir();
	assert.throws(
		() => resolveRealRenderer(root),
		(err) =>
			err instanceof UnavailableError &&
			JSON.stringify(err.missing.map((d) => d.issue)) ===
				JSON.stringify(['#9774', '#9793', '#9840'])
	);
	for (const d of REAL_PATH_DEPENDENCIES) {
		mkdirSync(dirname(join(root, d.path)), { recursive: true });
		writeFileSync(join(root, d.path), '');
	}
	assert.throws(
		() => resolveRealRenderer(root),
		(err) =>
			err instanceof UnavailableError && err.missing.length === 0 && /no adapter/.test(err.message)
	);
});

test('main() returns 1 naming the file on a veto, 0 on success', async (t) => {
	const errors = t.mock.method(console, 'error', () => {});
	t.mock.method(console, 'log', () => {});
	const vetoed = await main(['--out', freshDir(), '--sample-rate', String(SR)], {
		renderer: testDouble(spikeHard),
		allowTestDouble: true
	});
	assert.equal(vetoed, 1);
	const line = errors.mock.calls.map((c) => c.arguments[0]).find((l) => l.startsWith('[ERROR]'));
	assert.ok(line?.includes('file=clips/'), line);
	assert.equal(
		await main(['--out', freshDir(), '--sample-rate', String(SR)], {
			renderer: testDouble(),
			allowTestDouble: true
		}),
		0
	);
});
