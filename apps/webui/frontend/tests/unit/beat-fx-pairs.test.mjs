// requirement: BEATFX-10755
// The renderer in this file is a TEST DOUBLE: exporter coverage only, not calibration evidence.
// [if] exportBeatFxRatingPairs completes [then] 24 WAVs and a valid blind manifest exist, [else stop]
// [if] pairs.json exposes variant ids or proxies [then] blinding is broken, [else stop]
// [if] a question lacks 2 of 4 pairs with the first variant on side a [then] balanced sides broke, [else stop]
// [if] proxies fail to discriminate laws, depth or trails [then] measurement plumbing broke, [else stop]
// [if] peak, finite or click mutants write files [then] vetoes are broken, [else stop]
// [if] the CLI missing real path still creates --out [then] UNAVAILABLE gate broke, [else stop]
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
import { after, test } from 'node:test';

const SCRIPT_URL = new URL(
	'../../../../../scripts/export-beat-fx-rating-pairs.mjs',
	import.meta.url
);
const {
	exportBeatFxRatingPairs,
	assertBeatFxPairsManifestShape,
	assertFiniteBeatFxProxies,
	resolveRealRenderer,
	main,
	QUESTIONS,
	FIXTURE_NAMES,
	CLIP_BEATS,
	VetoError,
	UnavailableError,
	REAL_PATH_DEPENDENCIES,
	DEFAULT_SEED,
	BPM,
	TRAILS_LEVEL
} = await import(SCRIPT_URL.href);

const SR = 8000;
const _beatToSample = (beats, bpm, sr) => Math.round(((beats * 60) / bpm) * sr);
const FRAMES = _beatToSample(CLIP_BEATS, BPM, SR);

const G_WET = 0.09;
const K_DUCK = 0.45;
const FB = 0.32;

const testDouble = (fault = () => {}) => ({
	id: 'test-double-echo',
	realPath: false,
	async render(req) {
		const { variant, params, input, sampleRate, bpm, onSample, offSample } = req;
		const level = params.level;
		const law = params.law;
		const offMode = params.off;
		const cutMs = params.cutMs ?? 12;
		const n = input[0].length;
		const trailsPair = level === TRAILS_LEVEL && (variant === 'ring-out' || variant === 'cut-12ms');
		const delay = Math.max(1, _beatToSample(0.75, bpm, sampleRate));
		const ramp = Math.max(1, Math.round((20 * sampleRate) / 1000));
		const cutRamp = Math.max(1, Math.round((cutMs * sampleRate) / 1000));
		const buf = [new Float64Array(delay), new Float64Array(delay)];
		let wi = 0;
		const wet = [new Float64Array(n), new Float64Array(n)];
		const out = [new Float64Array(n), new Float64Array(n)];
		const smooth = (i, a, b, from, len) => {
			if (i < from) return a;
			if (i >= from + len) return b;
			const t = (i - from + 1) / len;
			const s = t * t * (3 - 2 * t);
			return a + (b - a) * s;
		};
		const wetGain = trailsPair ? G_WET * 1.4 : G_WET;
		const wetMix = (echo, lv) => wetGain * lv * echo;
		const duckGain = (i) => {
			if (law !== 'balance' || level === 0) return 1;
			const target = 1 - level * K_DUCK;
			if (i < onSample) return 1;
			if (i < offSample) return smooth(i, 1, target, onSample, ramp);
			return smooth(i, target, 1, offSample, ramp);
		};
		const inputGain = (i) => {
			if (i < onSample) return 0;
			if (i < offSample) return smooth(i, 0, 1, onSample, ramp);
			return smooth(i, 1, 0, offSample, ramp);
		};
		const wetOutGain = (i) => {
			if (i < onSample) return 0;
			if (i < offSample) return smooth(i, 0, 1, onSample, ramp);
			if (offMode === 'cut') {
				if (i < offSample + Math.max(2, cutRamp)) {
					return smooth(i, 1, 0, offSample, Math.max(2, cutRamp));
				}
				return 0;
			}
			return 1;
		};
		for (let i = 0; i < n; i++) {
			for (let c = 0; c < 2; c++) {
				const dry = input[c][i];
				const ig = inputGain(i);
				const delayed = buf[c][wi];
				const fb = trailsPair ? (offMode === 'ring-out' ? 0.68 : 0.14) : FB;
				const echo = ig * dry + fb * delayed;
				buf[c][wi] = echo;
				const wm = wetMix(echo, level) * wetOutGain(i);
				wet[c][i] = wm;
				out[c][i] = duckGain(i) * dry + wm;
			}
			wi = (wi + 1) % delay;
		}
		fault(req, out, wet);
		return { out, wet };
	}
});

const dirs = [];
const freshDir = () => {
	const d = mkdtempSync(join(tmpdir(), 'beat-fx-pairs-'));
	dirs.push(d);
	return d;
};
const run = (opts = {}) =>
	exportBeatFxRatingPairs({
		outDir: freshDir(),
		renderer: testDouble(),
		allowTestDouble: true,
		sampleRate: SR,
		...opts
	});

after(() => {
	for (const d of dirs) rmSync(d, { recursive: true, force: true });
});

test('export writes 24 clips and manifest shape', async () => {
	const dir = freshDir();
	const { manifest } = await exportBeatFxRatingPairs({
		outDir: dir,
		renderer: testDouble(),
		allowTestDouble: true,
		sampleRate: SR
	});
	assertBeatFxPairsManifestShape(manifest);
	assert.equal(readdirSync(join(dir, 'clips')).length, 24);
	const counts = {};
	for (const p of manifest.pairs) {
		const k = `${p.meta.question_key}:${p.meta.fixture}`;
		counts[k] = (counts[k] ?? 0) + 1;
	}
	assert.equal(Object.keys(counts).length, 12);
});

test('blind manifest and answer key variants', async () => {
	const dir = freshDir();
	await run({ outDir: dir });
	const manifestText = readFileSync(join(dir, 'pairs.json'), 'utf8');
	const key = JSON.parse(readFileSync(join(dir, 'answer-key.json'), 'utf8'));
	for (const q of Object.values(QUESTIONS)) {
		for (const vid of Object.keys(q.variants)) {
			assert.ok(!manifestText.includes(vid), `${vid} in manifest`);
		}
	}
	assert.ok(!manifestText.includes(String(key.seed)), 'seed in manifest');
	for (const p of Object.values(key.pairs)) {
		assert.notEqual(p.a.variant, p.b.variant);
	}
});

test('balanced sides per question', async () => {
	const dir = freshDir();
	const { answerKey } = await exportBeatFxRatingPairs({
		outDir: dir,
		renderer: testDouble(),
		allowTestDouble: true,
		sampleRate: SR
	});
	for (const qKey of Object.keys(QUESTIONS)) {
		const first = Object.keys(QUESTIONS[qKey].variants)[0];
		const pairs = Object.values(answerKey.pairs).filter((p) => p.question_key === qKey);
		const onA = pairs.filter((p) => p.a.variant === first).length;
		assert.equal(onA, 2, qKey);
	}
});

test('proxies discriminate under the double', async () => {
	const { answerKey } = await run();
	for (const p of Object.values(answerKey.pairs)) {
		for (const side of [p.a, p.b]) {
			assert.ok(Number.isFinite(side.loudness_change_lu));
			assert.ok(Number.isFinite(side.wet_dry_db));
			assert.ok(Number.isFinite(side.tail_ratio_db));
			assert.ok(Number.isFinite(side.click_ratio));
		}
	}
	const by = (qKey, fixture) =>
		Object.values(answerKey.pairs).find((p) => p.question_key === qKey && p.fixture === fixture);
	for (const fixture of FIXTURE_NAMES) {
		const ll = by('level-law', fixture);
		const dry = ll.a.variant === 'dry-held' ? ll.a : ll.b;
		const bal = ll.a.variant === 'balance' ? ll.a : ll.b;
		assert.ok(dry.loudness_change_lu > bal.loudness_change_lu, `level law LU ${fixture}`);
		const dd = by('default-depth', fixture);
		const shallow = dd.a.variant === 'depth-0.35' ? dd.a : dd.b;
		const deep = dd.a.variant === 'depth-0.5' ? dd.a : dd.b;
		assert.ok(deep.wet_dry_db > shallow.wet_dry_db, `depth wet/dry ${fixture}`);
		const tr = by('trails', fixture);
		const ring = tr.a.variant === 'ring-out' ? tr.a : tr.b;
		const cut = tr.a.variant === 'cut-12ms' ? tr.a : tr.b;
		const trailSep = ring.tail_ratio_db - cut.tail_ratio_db;
		const trailSepMinByFixture = {
			'kick-bass': 6,
			'hats-perc': 8,
			'house-124': 12,
			'chord-stab': 20
		};
		const trailSepMin = trailSepMinByFixture[fixture] ?? 20;
		assert.ok(
			trailSep >= trailSepMin,
			`trails tail ${fixture} ring=${ring.tail_ratio_db} cut=${cut.tail_ratio_db} sep=${trailSep}`
		);
		assert.ok(cut.tail_ratio_db < ring.tail_ratio_db, `cut shorter than ring ${fixture}`);
	}
});

test('WAV format at 8 kHz', async () => {
	const dir = freshDir();
	await run({ outDir: dir });
	for (const f of readdirSync(join(dir, 'clips'))) {
		const buf = readFileSync(join(dir, 'clips', f));
		assert.equal(buf.toString('ascii', 0, 4), 'RIFF');
		assert.equal(buf.readUInt16LE(22), 2);
		assert.equal(buf.readUInt32LE(24), SR);
		assert.equal(buf.readUInt16LE(34), 16);
		assert.equal(buf.readUInt32LE(40) / 4, FRAMES);
	}
});

test('idempotent seed and different seed changes layout', async () => {
	const d1 = freshDir();
	const d2 = freshDir();
	await run({ outDir: d1, seed: DEFAULT_SEED });
	await run({ outDir: d2, seed: DEFAULT_SEED });
	assert.ok(readFileSync(join(d1, 'pairs.json')).equals(readFileSync(join(d2, 'pairs.json'))));
	assert.ok(
		readFileSync(join(d1, 'answer-key.json')).equals(readFileSync(join(d2, 'answer-key.json')))
	);
	for (const f of readdirSync(join(d1, 'clips'))) {
		assert.ok(readFileSync(join(d1, 'clips', f)).equals(readFileSync(join(d2, 'clips', f))), f);
	}
	const otherDir = freshDir();
	await run({ outDir: otherDir, seed: 42 });
	const base = JSON.parse(readFileSync(join(d1, 'answer-key.json'), 'utf8'));
	const other = JSON.parse(readFileSync(join(otherDir, 'answer-key.json'), 'utf8'));
	assert.notEqual(other.seed, base.seed);
	const differs =
		JSON.stringify(Object.keys(other.pairs)) !== JSON.stringify(Object.keys(base.pairs)) ||
		Object.keys(other.pairs).some((id) => other.pairs[id].a.variant !== base.pairs[id].a.variant);
	assert.ok(differs, 'different seed should change layout');
});

test('peak veto writes nothing', async () => {
	const dir = freshDir();
	await assert.rejects(
		exportBeatFxRatingPairs({
			outDir: dir,
			renderer: testDouble((req, out) => {
				out[0][100] *= 1.5;
			}),
			allowTestDouble: true,
			sampleRate: SR
		}),
		(err) =>
			err instanceof VetoError && err.veto === 'peak' && /clips\/p\d{2}-[ab]\.wav/.test(err.file)
	);
	assert.ok(!existsSync(join(dir, 'clips')));
});

test('finite veto writes nothing', async () => {
	const dir = freshDir();
	await assert.rejects(
		exportBeatFxRatingPairs({
			outDir: dir,
			renderer: testDouble((req, out) => {
				out[1][50] = NaN;
			}),
			allowTestDouble: true,
			sampleRate: SR
		}),
		(err) => err instanceof VetoError && err.veto === 'finite'
	);
	assert.deepEqual(existsSync(dir) ? readdirSync(dir) : [], []);
});

test('click veto on hard step at OFF', async () => {
	const dir = freshDir();
	const off = _beatToSample(28, BPM, SR);
	await assert.rejects(
		exportBeatFxRatingPairs({
			outDir: dir,
			renderer: testDouble((req, out) => {
				if (req.params.off === 'cut') out[0][off + 50] += 0.4;
			}),
			allowTestDouble: true,
			sampleRate: SR
		}),
		(err) => err instanceof VetoError && err.veto === 'click' && err.measured > 1.5
	);
	assert.deepEqual(existsSync(dir) ? readdirSync(dir) : [], []);
});

test('renderer gate refuses test double without flag', async () => {
	await assert.rejects(
		exportBeatFxRatingPairs({ outDir: freshDir(), renderer: testDouble(), sampleRate: SR }),
		/#9774/
	);
});

test('contract rejects missing wet', async () => {
	await assert.rejects(
		exportBeatFxRatingPairs({
			outDir: freshDir(),
			renderer: {
				id: 'broken',
				realPath: false,
				async render(req) {
					return { out: req.input };
				}
			},
			allowTestDouble: true,
			sampleRate: SR
		}),
		/broke the contract/
	);
});

test('contract rejects one-channel out', async () => {
	await assert.rejects(
		exportBeatFxRatingPairs({
			outDir: freshDir(),
			renderer: {
				id: 'broken-one-ch',
				realPath: false,
				async render(req) {
					return { out: [req.input[0]], wet: req.input };
				}
			},
			allowTestDouble: true,
			sampleRate: SR
		}),
		/broke the contract/
	);
});

test('non-finite proxy throws before write', () => {
	assert.throws(
		() =>
			assertFiniteBeatFxProxies(
				{ loudness_change_lu: 0, wet_dry_db: Number.NaN, tail_ratio_db: -40 },
				'kick-bass'
			),
		/non-finite proxy wet_dry_db/
	);
});

test('non-empty --out refused', async () => {
	const dir = freshDir();
	writeFileSync(join(dir, 'stray.txt'), 'x');
	await assert.rejects(run({ outDir: dir }), /not empty/);
});

test('main refuses non-empty --out and leaves existing file', async () => {
	const dir = freshDir();
	const sentinel = join(dir, 'keep.txt');
	writeFileSync(sentinel, 'sentinel');
	const code = await main(['--out', dir, '--sample-rate', String(SR)], {
		renderer: testDouble(),
		allowTestDouble: true
	});
	assert.equal(code, 1);
	assert.equal(readFileSync(sentinel, 'utf8'), 'sentinel');
});

test('CLI without --out exits 1', () => {
	const r = spawnSync(process.execPath, [fileURLToPath(SCRIPT_URL)], { encoding: 'utf8' });
	assert.equal(r.status, 1);
	assert.match(r.stderr, /^\[ERROR\].*--out/);
});

test('CLI unavailable does not create --out', () => {
	const out = join(freshDir(), 'probe');
	const r = spawnSync(process.execPath, [fileURLToPath(SCRIPT_URL), '--out', out], {
		encoding: 'utf8'
	});
	assert.equal(r.status, 2);
	assert.match(r.stderr, /^\[UNAVAILABLE\]/);
	// #9782 (beat-fx-unit.ts) is present since PR #10722, so only the #9774 harness is missing.
	assert.ok(r.stderr.includes('#9774'));
	assert.equal(existsSync(out), false);
});

test('resolveRealRenderer lists dependencies', () => {
	const root = freshDir();
	assert.throws(
		() => resolveRealRenderer(root),
		(err) =>
			err instanceof UnavailableError && err.missing.map((d) => d.issue).join() === '#9774,#9782'
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

test('main veto and success codes', async (t) => {
	const errMock = t.mock.method(console, 'error', () => {});
	t.mock.method(console, 'log', () => {});
	const vetoed = await main(['--out', freshDir(), '--sample-rate', String(SR)], {
		renderer: testDouble((req, out) => {
			out[0][200] = 5;
		}),
		allowTestDouble: true
	});
	assert.equal(vetoed, 1);
	const line = errMock.mock.calls.map((c) => c.arguments[0]).find((l) => l.startsWith('[ERROR]'));
	assert.ok(line?.includes('file=clips/'), line);
	assert.equal(
		await main(['--out', freshDir(), '--sample-rate', String(SR)], {
			renderer: testDouble(),
			allowTestDouble: true
		}),
		0
	);
});

test('48 kHz export completes under 60 s', async (t) => {
	const t0 = performance.now();
	await exportBeatFxRatingPairs({
		outDir: freshDir(),
		renderer: testDouble(),
		allowTestDouble: true,
		sampleRate: 48000
	});
	const sec = (performance.now() - t0) / 1000;
	t.diagnostic(`beat-fx double export at 48 kHz: ${sec.toFixed(2)} s`);
	assert.ok(sec < 60);
});
