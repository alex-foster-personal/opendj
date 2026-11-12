// requirement: FXECHO-10720
// TEST DOUBLE only; calibration uses the real kernels via #9774.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';

const SCRIPT_URL = new URL(
	'../../../../../scripts/export-time-fx-rating-pairs.mjs',
	import.meta.url
);
const {
	exportTimeFxRatingPairs,
	assertTimeFxPairsManifestShape,
	validateManifestSchema,
	ManifestSchemaError,
	MissingVariantError,
	CANONICAL_PAIRS,
	VARIANTS,
	KERNELS,
	DEFAULT_SEED,
	REAL_PATH_DEPENDENCIES,
	resolveRealRenderer,
	UnavailableError,
	clipSeconds,
	listeningSeconds,
	LUFS_MATCH_MAX_DB,
	LISTENING_MAX_SEC,
	SEND_ON_BEAT,
	SEND_OFF_BEAT,
	CLIP_BARS,
	BPM
} = await import(SCRIPT_URL.href);

const SR = 12000;
const expectedFrames = Math.round(((CLIP_BARS * 4 * 60) / BPM) * SR);

const SUPPORTED = {
	pingpong: new Set(['pingpongWidth']),
	spiral: new Set(['spiralVariant']),
	multitap: new Set(['multitapLaw']),
	tape: new Set(['tapeMotion'])
};

const testDouble = (supported = SUPPORTED) => ({
	id: 'test-double:time-fx',
	realPath: false,
	supportedOptions: (kernel) => supported[kernel] ?? new Set(),
	async render({ kernel, variant, options, input, sendOnSample, sendOffSample, beat, depth }) {
		const wet = depth ?? 0.55;
		return input.map((ch, c) => {
			const n = ch.length;
			const out = new Float64Array(n);
			const echo = (i, d) => ch[Math.max(0, i - d)] ?? 0;
			const delay = Math.max(1, Math.round(((beat ?? 0.75) * 60 * SR) / BPM));
			for (let i = 0; i < n; i++) {
				const send = i >= sendOnSample && i < sendOffSample ? 1 : 0;
				let fx = ch[i];
				if (kernel === 'pingpong') {
					const w = options.pingpongWidth ?? 1;
					const side = (c ^ Math.floor(i / delay)) & 1 ? w : 1 - w;
					fx = ch[i] + send * side * 0.35 * echo(i, delay);
				} else if (kernel === 'spiral') {
					const density = { light: 0.2, medium: 0.4, dense: 0.65 }[options.spiralVariant] ?? 0.3;
					let sum = 0;
					for (let k = 1; k <= 4; k++) sum += echo(i, delay * k) * density ** k;
					fx = ch[i] + send * wet * sum;
				} else if (kernel === 'multitap') {
					const law = options.multitapLaw === 'flat' ? 1 : 0.85;
					for (let k = 1; k <= 3; k++) fx += send * wet * law ** (k - 1) * echo(i, (delay * k) / 2);
				} else {
					const drift = { off: 0, subtle: 0.02, heavy: 0.06 }[options.tapeMotion] ?? 0;
					fx = ch[i] + send * wet * echo(i, delay) * (1 + drift * Math.sin(i / 200));
				}
				out[i] = ch[i] * (1 - 0.45 * send * wet) + fx * 0.45 * send * wet;
			}
			if (variant === 'heavy' && c === 0) out[sendOnSample + 10] *= 1.08;
			return out;
		});
	}
});

const dirs = [];
const freshDir = () => {
	const d = mkdtempSync(join(tmpdir(), 'time-fx-pairs-'));
	dirs.push(d);
	return d;
};
after(() => dirs.forEach((d) => rmSync(d, { recursive: true, force: true })));

const OPTS = { sampleRate: SR, renderer: testDouble(), allowTestDouble: true };
let outDir;
let manifest;
let blindKey;
before(async () => {
	outDir = freshDir();
	manifest = await exportTimeFxRatingPairs({ outDir, ...OPTS });
	blindKey = JSON.parse(readFileSync(join(outDir, 'blind_key.json'), 'utf8'));
});

test('[if] canonical table is wrong [then] pair coverage breaks blind rating, [else stop]', () => {
	assert.equal(CANONICAL_PAIRS.length, 10);
	assert.equal(manifest.pairs.length, 10);
	const counts = Object.fromEntries(KERNELS.map((k) => [k, 0]));
	for (const p of manifest.pairs) counts[p.meta.kernel]++;
	assert.deepEqual(counts, { pingpong: 2, spiral: 3, multitap: 2, tape: 3 });
	for (const spec of CANONICAL_PAIRS) {
		const row = manifest.pairs.find((p) => p.meta.key === spec.key);
		assert.ok(row, spec.key);
		assert.equal(row.meta.kernel, spec.kernel);
		if (spec.beat != null) assert.equal(row.meta.beat, spec.beat);
		if (spec.depth != null) assert.equal(row.meta.depth, spec.depth);
		assert.deepEqual(new Set([row.meta.a_variant, row.meta.b_variant]), new Set([spec.a, spec.b]));
	}
	assert.equal(readdirSync(join(outDir, 'clips')).length, 20);
	for (const p of manifest.pairs) {
		const blob = `${p.id}${p.a}${p.b}${p.question}`;
		for (const label of Object.keys(VARIANTS[p.meta.kernel])) assert.ok(!blob.includes(label));
	}
	assertTimeFxPairsManifestShape(manifest);
});

test('[if] clips are not 8 bars send-gated or LUFS match over 0.5 LU [then] session is unfair, [else stop]', () => {
	assert.equal(manifest.samples, expectedFrames);
	for (const p of manifest.pairs) {
		assert.equal(p.meta.send_on_beat, SEND_ON_BEAT);
		assert.equal(p.meta.send_off_beat, SEND_OFF_BEAT);
		assert.ok(p.meta.lufs_delta_db <= LUFS_MATCH_MAX_DB, p.id);
		for (const rel of [p.a, p.b]) {
			const wav = readFileSync(join(outDir, rel));
			assert.equal(wav.readUInt32LE(40) / 4, expectedFrames);
			assert.equal(wav.readUInt16LE(22), 2);
			assert.notEqual(wav.readInt16LE(44), 0);
		}
	}
	const listen = listeningSeconds(10, BPM, CLIP_BARS);
	assert.equal(manifest.listening_seconds, listen);
	assert.ok(listen < LISTENING_MAX_SEC);
	assert.ok(clipSeconds(BPM, CLIP_BARS) > 14);
});

test('[if] seed blind_key drifts from manifest or schema fails unnamed [then] ratings cannot map, [else stop]', async () => {
	assert.equal(manifest.seed, DEFAULT_SEED);
	assert.equal(blindKey.seed, DEFAULT_SEED);
	assert.equal(blindKey.pairs.length, 10);
	for (const p of manifest.pairs) {
		const key = blindKey.pairs.find((k) => k.id === p.id);
		assert.deepEqual(key, {
			id: p.id,
			a_variant: p.meta.a_variant,
			b_variant: p.meta.b_variant,
			a: p.a,
			b: p.b
		});
	}
	assert.deepEqual(manifest.hidden_fields, ['meta']);
	assert.ok(existsSync(join(outDir, 'blind_key.json')));
	validateManifestSchema(manifest);
	assert.throws(
		() => validateManifestSchema({ family: 'time-fx', pairs: [] }),
		(err) => {
			assert.equal(err.name, 'ManifestSchemaError');
			return true;
		}
	);
	const again = await exportTimeFxRatingPairs({ outDir: freshDir(), ...OPTS });
	assert.deepEqual(again, manifest);
	const other = await exportTimeFxRatingPairs({ outDir: freshDir(), ...OPTS, seed: 0xdeadbeef });
	assert.notEqual(other.seed, manifest.seed);
	assert.ok(
		other.pairs.some(
			(p, i) => p.a !== manifest.pairs[i].a || p.meta.a_variant !== manifest.pairs[i].meta.a_variant
		),
		'different seed must change blinding'
	);
});

test('[if] renderer lacks a variant option or CLI is UNAVAILABLE [then] export must stop, [else stop]', async () => {
	const broken = testDouble({ tape: new Set(['tapeMotion']) });
	await assert.rejects(
		() => exportTimeFxRatingPairs({ outDir: freshDir(), ...OPTS, renderer: broken }),
		(err) => err instanceof MissingVariantError && err.name === 'MissingVariantError'
	);
	const noOut = spawnSync(process.execPath, [fileURLToPath(SCRIPT_URL)], { encoding: 'utf8' });
	assert.equal(noOut.status, 1);
	assert.match(noOut.stderr, /^\[ERROR\].*--out/);
	const probe = join(tmpdir(), `time-fx-cli-${Date.now()}`);
	const unavail = spawnSync(process.execPath, [fileURLToPath(SCRIPT_URL), '--out', probe], {
		encoding: 'utf8'
	});
	assert.equal(unavail.status, 2);
	assert.match(unavail.stderr, /^\[UNAVAILABLE\]/);
	assert.equal(existsSync(probe), false);
	assert.throws(
		() => resolveRealRenderer(freshDir()),
		(err) => err instanceof UnavailableError && err.missing.length === REAL_PATH_DEPENDENCIES.length
	);
	const once = spawnSync(process.execPath, [fileURLToPath(SCRIPT_URL), '--out', freshDir()], {
		encoding: 'utf8'
	});
	assert.equal(once.status, 2);
});
