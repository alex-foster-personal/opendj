// requirement: FXCAL-10673
// The renderer in this file is a TEST DOUBLE (coverage only, not calibration evidence).
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

const SCRIPT_URL = new URL('../../scripts/export-buffer-space-fx-pairs.mjs', import.meta.url);
const mod = await import(SCRIPT_URL.href);
const {
	exportBufferSpaceFxPairs,
	assertBufferSpacePairsManifestShape,
	canonicalPairs,
	shortTermLufs,
	levelMatchPair,
	VARIANTS,
	PAIR_COUNTS,
	KERNELS,
	FIXTURE_NAMES,
	DEFAULT_SEED,
	REAL_PATH_DEPENDENCIES,
	resolveRealRenderer,
	UnavailableError,
	LUFS_MATCH_MAX_DB
} = mod;

const SR = 12000;
const BARS = 4;

const testDouble = () => ({
	id: 'test-double:buffer-space',
	realPath: false,
	async render({ kernel, axis, variant, constants, input, depth, pitchSemitones }) {
		const wet = depth ?? 0.7;
		const dry = 1 - wet;
		return input.map((ch, c) => {
			const n = ch.length;
			const out = new Float64Array(n);
			for (let i = 0; i < n; i++) {
				let fx = ch[i];
				if (kernel === 'PITCH') {
					const w = constants.windowMs ?? 40;
					const st = pitchSemitones ?? 0;
					const mod = Math.sin((2 * Math.PI * i) / (SR * (w / 1000) + 1));
					fx = ch[i] * (1 + 0.08 * st * mod * (w / 40));
				} else if (kernel === 'SHIMMER') {
					if (axis === 'interval') {
						const st = constants.semitones ?? 12;
						fx = ch[i] + 0.12 * Math.sin((2 * Math.PI * (220 * 2 ** (st / 12)) * i) / SR) * wet;
					} else {
						const rate = constants.sparkleRate ?? 0.5;
						fx = ch[i] * (1 + rate * 0.15 * Math.sin((2 * Math.PI * 3 * i) / SR));
					}
				} else if (kernel === 'STUTTER') {
					if (axis === 'chunk') {
						const beat = (60 / 124) * SR;
						const chunk = Math.max(1, Math.round((constants.chunkBeats ?? 0.25) * beat));
						const rep = ch[i - (i % chunk)] ?? ch[0];
						fx = dry * ch[i] + wet * rep;
					} else if (constants.blend === 'equal-power') {
						const g = Math.sin((wet * Math.PI) / 2);
						const d = Math.cos((wet * Math.PI) / 2);
						fx = d * ch[i] + g * ch[Math.max(0, i - Math.round(SR / 20))];
					} else {
						fx = Math.max(ch[i], ch[Math.max(0, i - Math.round(SR / 20))] * wet);
					}
				}
				out[i] = dry * ch[i] + wet * fx;
			}
			return out;
		});
	}
});

const dirs = [];
const freshDir = () => {
	const d = mkdtempSync(join(tmpdir(), 'bufspace-pairs-'));
	dirs.push(d);
	return d;
};
after(() => dirs.forEach((d) => rmSync(d, { recursive: true, force: true })));

const OPTS = { sampleRate: SR, bars: BARS, renderer: testDouble(), allowTestDouble: true };
let outDir;
let manifest;
before(async () => {
	outDir = freshDir();
	manifest = await exportBufferSpaceFxPairs({ outDir, ...OPTS });
});

test('[if] variants lack harness manifests, blind labels or sidecar keys [then] export is unusable, [else stop]', () => {
	assertBufferSpacePairsManifestShape(manifest);
	assert.equal(manifest.seed, DEFAULT_SEED);
	assert.deepEqual(manifest.renderer, { id: 'test-double:buffer-space', real_path: false });
	assert.deepEqual(manifest.variants, VARIANTS);
	assert.equal(manifest.pairs.length, 16);
	const leak = new Set(
		Object.keys(VARIANTS.PITCH).concat(
			'+12',
			'+7',
			'0.5',
			'0.8',
			'1/4',
			'1/8',
			'equal-power',
			'dry-held'
		)
	);
	for (const p of manifest.pairs) {
		assert.ok(p.meta.variant_setting.startsWith('variant_fx_'));
		assert.deepEqual(p.meta.variant_key, { a: p.meta.a_variant, b: p.meta.b_variant });
		const blob = `${p.a}${p.b}${p.question}`;
		for (const id of leak) assert.ok(!blob.includes(id), `${p.id} leaks ${id}`);
	}
	assert.deepEqual(JSON.parse(readFileSync(join(outDir, 'pairs.json'), 'utf8')), manifest);
	assert.equal(readdirSync(join(outDir, 'clips')).length, 32);
});

test('[if] paired clips short-term LUFS differ by more than 0.5 dB [then] loudness bias breaks blind picks, [else stop]', () => {
	for (const p of manifest.pairs) {
		assert.ok(p.meta.lufs_delta_db <= LUFS_MATCH_MAX_DB, `${p.id} delta ${p.meta.lufs_delta_db}`);
		const decode = (rel) => {
			const wav = readFileSync(join(outDir, rel));
			const frames = wav.readUInt32LE(40) / 4;
			const ch = [new Float64Array(frames), new Float64Array(frames)];
			for (let i = 0; i < frames; i++) {
				ch[0][i] = wav.readInt16LE(44 + i * 4) / 32768;
				ch[1][i] = wav.readInt16LE(44 + i * 4 + 2) / 32768;
			}
			return ch;
		};
		const lufsA = shortTermLufs(decode(p.a), SR);
		const lufsB = shortTermLufs(decode(p.b), SR);
		assert.ok(Math.abs(lufsA - lufsB) <= LUFS_MATCH_MAX_DB + 0.05, `${p.id} remeasured`);
	}
	const probeLen = Math.round(SR * 0.5);
	const tone = (g) => [
		Float64Array.from({ length: probeLen }, (_, i) => g * Math.sin((2 * Math.PI * 440 * i) / SR)),
		Float64Array.from({ length: probeLen }, (_, i) => g * Math.cos((2 * Math.PI * 440 * i) / SR))
	];
	const matched = levelMatchPair(tone(0.2), tone(0.05), SR);
	assert.ok(matched.lufs_delta_db <= LUFS_MATCH_MAX_DB);
});

test('[if] pair count per kernel is wrong or a registered variant is missing [then] coverage is incomplete, [else stop]', () => {
	for (const kernel of KERNELS) {
		const ps = manifest.pairs.filter((p) => p.meta.kernel === kernel);
		assert.equal(ps.length, PAIR_COUNTS[kernel], kernel);
	}
	const used = { PITCH: new Set(), SHIMMER: new Set(), STUTTER: new Set() };
	for (const p of manifest.pairs) {
		used[p.meta.kernel].add(p.meta.a_variant);
		used[p.meta.kernel].add(p.meta.b_variant);
		assert.ok(FIXTURE_NAMES.includes(p.meta.fixture));
	}
	for (const k of Object.keys(VARIANTS.PITCH)) assert.ok(used.PITCH.has(k), k);
	for (const axis of ['interval', 'sparkle']) {
		for (const k of Object.keys(VARIANTS.SHIMMER[axis])) assert.ok(used.SHIMMER.has(k), k);
	}
	for (const axis of ['chunk', 'blend']) {
		for (const k of Object.keys(VARIANTS.STUTTER[axis])) assert.ok(used.STUTTER.has(k), k);
	}
	assert.equal(canonicalPairs().length, 16);
});

test('[if] same seed writes different bytes or CLI needs the app [then] offline idempotence failed, [else stop]', async () => {
	const again = await exportBufferSpaceFxPairs({ outDir: freshDir(), ...OPTS });
	assert.deepEqual(again, manifest);
	const other = await exportBufferSpaceFxPairs({ outDir: freshDir(), ...OPTS, seed: 0xdeadbeef });
	const layout = (m) => m.pairs.map((p) => `${p.meta.kernel}:${p.meta.a_variant}:${p.a}`).join('|');
	assert.notEqual(layout(other), layout(manifest));
	const noOut = spawnSync(process.execPath, [fileURLToPath(SCRIPT_URL)], { encoding: 'utf8' });
	assert.equal(noOut.status, 1);
	assert.match(noOut.stderr, /^\[ERROR\].*--out/);
	const out = join(freshDir(), 'probe');
	const unavail = spawnSync(process.execPath, [fileURLToPath(SCRIPT_URL), '--out', out], {
		encoding: 'utf8'
	});
	assert.equal(unavail.status, 2);
	assert.match(unavail.stderr, /^\[UNAVAILABLE\]/);
	for (const issue of REAL_PATH_DEPENDENCIES.map((d) => d.issue))
		assert.ok(unavail.stderr.includes(issue));
	assert.equal(existsSync(out), false);
	assert.throws(
		() => resolveRealRenderer(freshDir()),
		(err) => {
			assert.ok(err instanceof UnavailableError);
			assert.equal(err.missing.length, REAL_PATH_DEPENDENCIES.length);
			return true;
		}
	);
});
