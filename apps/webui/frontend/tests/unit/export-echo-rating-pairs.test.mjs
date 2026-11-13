// requirement: FXECHO-14472
// TEST DOUBLE only; calibration uses the shipped echo kernel via #9774.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';

const SCRIPT_URL = new URL('../../../../../scripts/export-echo-rating-pairs.mjs', import.meta.url);
const {
	exportEchoRatingPairs,
	validateManifestSchema,
	ManifestSchemaError,
	MissingVariantError,
	UnavailableError,
	resolveRealRenderer,
	CANONICAL_PAIRS,
	VARIANTS,
	DEFAULT_SEED,
	REAL_PATH_DEPENDENCIES,
	RETUNE,
	LUFS_MATCH_MAX_DB,
	LISTENING_MAX_SEC,
	SEND_ON_BEAT,
	SEND_OFF_BEAT,
	CLIP_BARS,
	BPM,
	listeningSeconds
} = await import(SCRIPT_URL.href);

const SR = 12000;
const FRAMES = Math.round(((CLIP_BARS * 4 * 60) / BPM) * SR);
const OPTION_KEYS = new Set(['mode', 'beat', 'feedback', 'loopHpHz', 'loopLpHz', 'tapeGlideSec']);

/** A feedback delay line with a one-pole loop LP: enough to give each variant a distinct, bounded tail. */
const testDouble = (supported = OPTION_KEYS, requests = []) => ({
	id: 'test-double:echo',
	realPath: false,
	supportedOptions: () => supported,
	async render(req) {
		requests.push(req);
		const { options, input, sendOnSample, sendOffSample, sampleRate } = req;
		const d = Math.round(((options.beat * 60) / req.bpm) * sampleRate);
		const k = 1 - Math.exp((-2 * Math.PI * options.loopLpHz) / sampleRate);
		return input.map((ch) => {
			const wet = new Float64Array(ch.length);
			let lp = 0;
			for (let i = 0; i < ch.length; i++) {
				const send = i >= sendOnSample && i < sendOffSample ? ch[i] : 0;
				lp += k * ((i >= d ? wet[i - d] : 0) - lp);
				wet[i] = send + options.feedback * lp;
			}
			return Float64Array.from(ch, (x, i) => 0.5 * (x + wet[i]));
		});
	}
});

const dirs = [];
const freshDir = () => {
	const d = mkdtempSync(join(tmpdir(), 'echo-pairs-'));
	dirs.push(d);
	return d;
};
after(() => dirs.forEach((d) => rmSync(d, { recursive: true, force: true })));

const requests = [];
const OPTS = { sampleRate: SR, renderer: testDouble(OPTION_KEYS, requests), allowTestDouble: true };
let outDir;
let manifest;
before(async () => {
	outDir = freshDir();
	manifest = await exportEchoRatingPairs({ outDir, ...OPTS });
});
const runCli = (args) =>
	spawnSync(process.execPath, [fileURLToPath(SCRIPT_URL), ...args], { encoding: 'utf8' });

test('[if] pairs miss feedback at 3/4, division, 6 tone or tape glide rows [then] the maintainer rates the wrong voicing, [else stop].', () => {
	const counts = { feedback: 0, division: 0, tone: 0, tape: 0 };
	for (const p of manifest.pairs) counts[p.meta.axis]++;
	assert.deepEqual(counts, { feedback: 3, division: 1, tone: 6, tape: 3 });
	assert.deepEqual(Object.keys(VARIANTS.feedback), ['0.45', '0.55', '0.65', '0.75']);
	assert.equal(Object.keys(VARIANTS.tone).length, 9);
	for (const p of manifest.pairs.filter((x) => x.meta.axis === 'feedback'))
		assert.equal(p.meta.options.a.beat, 0.75);
	const division = manifest.pairs.find((x) => x.meta.axis === 'division').meta.options;
	assert.deepEqual(new Set([division.a.beat, division.b.beat]), new Set([0.5, 0.75]));
	const tape = Object.values(VARIANTS.tape);
	assert.deepEqual(
		tape.map((v) => v.tapeGlideSec ?? v.mode),
		[0.08, 0.25, 'echo']
	);
	for (const r of requests.filter((x) => x.axis === 'tape')) assert.deepEqual(r.retune, RETUNE);
	assert.equal(readdirSync(join(outDir, 'clips')).length, 2 * CANONICAL_PAIRS.length);
	for (const p of manifest.pairs)
		assert.ok(!`${p.id}${p.a}${p.b}${p.question}`.includes(p.meta.a_variant));
});

test('[if] clips are not 8 bars at 124 BPM, send 1 bar then shut, sides within 0.5 LU [then] session is unfair, [else stop].', () => {
	assert.equal(manifest.samples, FRAMES);
	for (const r of requests) {
		assert.equal(r.bpm, 124);
		assert.equal(r.sendOffSample - r.sendOnSample, Math.round(((4 * 60) / BPM) * SR));
		assert.equal(r.sendOnSample, Math.round(((SEND_ON_BEAT * 60) / BPM) * SR));
	}
	for (const p of manifest.pairs) {
		assert.equal(p.meta.send_on_beat, SEND_ON_BEAT);
		assert.equal(p.meta.send_off_beat, SEND_OFF_BEAT);
		assert.ok(p.meta.lufs_delta_db <= LUFS_MATCH_MAX_DB, p.id);
		for (const rel of [p.a, p.b]) {
			const wav = readFileSync(join(outDir, rel));
			assert.equal(wav.readUInt32LE(40) / 4, FRAMES);
			assert.equal(wav.readUInt16LE(22), 2);
		}
	}
});

test('[if] side order is not seeded or blind_key sits in the manifest [then] ratings leak or cannot map, [else stop].', async () => {
	const key = JSON.parse(readFileSync(join(outDir, 'blind_key.json'), 'utf8'));
	assert.equal(key.seed, DEFAULT_SEED);
	assert.deepEqual(manifest.hidden_fields, ['meta']);
	assert.ok(!('blind_key' in manifest));
	for (const p of manifest.pairs)
		assert.deepEqual(
			key.pairs.find((k) => k.id === p.id),
			{ id: p.id, a_variant: p.meta.a_variant, b_variant: p.meta.b_variant, a: p.a, b: p.b }
		);
	const again = await exportEchoRatingPairs({ outDir: freshDir(), ...OPTS });
	assert.deepEqual(again, manifest);
	const other = await exportEchoRatingPairs({ outDir: freshDir(), ...OPTS, seed: 0xdeadbeef });
	assert.ok(
		other.pairs.some(
			(p, i) =>
				p.meta.key !== manifest.pairs[i].meta.key ||
				p.meta.a_variant !== manifest.pairs[i].meta.a_variant
		)
	);
});

test('[if] a schema mismatch, a missing option or no render path passes unnamed [then] a bad round ships, [else stop].', async () => {
	validateManifestSchema(manifest);
	assert.throws(
		() => validateManifestSchema({ ...manifest, pairs: manifest.pairs.slice(1) }),
		(err) => err instanceof ManifestSchemaError && err.name === 'ManifestSchemaError'
	);
	const noTape = new Set([...OPTION_KEYS].filter((k) => k !== 'tapeGlideSec'));
	await assert.rejects(
		() => exportEchoRatingPairs({ outDir: freshDir(), ...OPTS, renderer: testDouble(noTape) }),
		(err) => err instanceof MissingVariantError && err.axis === 'tape'
	);
	assert.throws(
		() => resolveRealRenderer(freshDir()),
		(err) => err instanceof UnavailableError && err.missing.length === REAL_PATH_DEPENDENCIES.length
	);
	const probe = join(tmpdir(), `echo-cli-${process.pid}-${Date.now()}`);
	const unavail = runCli(['--out', probe]);
	assert.equal(unavail.status, 2);
	assert.match(unavail.stderr, /^\[UNAVAILABLE\].*#9774/);
	assert.equal(existsSync(probe), false);
	assert.equal(runCli([]).status, 1);
});

test('[if] total listening time is not printed or reaches 5 minutes [then] the session runs too long, [else stop].', () => {
	assert.equal(manifest.listening_seconds, listeningSeconds(CANONICAL_PAIRS.length));
	assert.ok(manifest.listening_seconds < LISTENING_MAX_SEC && LISTENING_MAX_SEC === 300);
	const out = runCli(['--out', join(tmpdir(), `echo-cli-listen-${process.pid}`)]);
	assert.match(
		out.stdout,
		new RegExp(`listening time ${manifest.listening_seconds} s for 13 pairs`)
	);
});
