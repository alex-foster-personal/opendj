// requirement: FXFILT-9829
// requirement: RB-PERFORMANCE-13546
// requirement: FXFILT-14361
// The renderer in this file is a TEST DOUBLE. It covers pairing, blinding, proxies, manifest, vetoes and
// WAV plumbing (test coverage); it is not calibration evidence. Calibration renders only through the
// #9774 harness on the real graph with #9826 and #9828.
// [if] the manifest fails assertPairsManifestShape or misses a named comparison, gesture or fixture [then] the filter export is broken
// [if] an id or clip path repeats, or a candidate label shows in id/a/b/question [then] blinding is broken
// [if] a clip is not 16-bit stereo PCM of the asked length, or a and b are identical [then] the WAV writer is broken
// [if] the same seed gives a different manifest, or sides never or always swap [then] seeded blinding is broken
// [if] a proxy is missing, non-finite or ignores glide time [then] ratings cannot be checked against proxies
// [if] meta candidates do not match the swapped requests [then] a rating cannot map back to constants
// [if] Q pairs do not use the winning glide or its source is not recorded [then] the Q round is mislabelled
// [if] a clip over 0 dBFS or holding NaN still writes files [then] the hard vetoes are not gating the export
// [if] assertPairsManifestShape accepts a ninth pair, a leak, a missing residual or a non-finite proxy [then] the shape check is toothless
// [if] the CLI runs without --out and does not exit 1 with [ERROR] [then] the CLI does not fail fast
// [if] the CLI receives an invalid argument [then] it exits 1 with [ERROR] and writes no calibration artifacts [else stop]
// [if] the real render path is missing and the CLI does not exit 2 with [UNAVAILABLE] or creates --out [then] calibration can run on a reimplementation
// [if] resolveRealRenderer does not name the missing dependencies, or passes without an adapter [then] the real-path gate is broken
// [if] a non-real or missing renderer is accepted without allowTestDouble [then] the real-path gate is broken
// [if] a render resolves with a valid result after the caller replaced the export [then] the export fails closed [else stop].
// [if] a held render on an export whose signal stays live is released [then] the export writes, so the hold returns a valid result [else stop].
// [if] the test fixture is released [then] in-flight exports finish before rmSync [else stop].
// [if] the export rejects its render result [then] --out stays absent [else stop].
// [if] the LFO sine/triangle or down/up pair is missing, splits its division or lacks the other proxies [then] LFO is unratable [else stop].
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SCRIPT_URL = new URL(
	'../../../../../scripts/export-filter-rating-pairs.mjs',
	import.meta.url
);
const mod = await import(SCRIPT_URL.href);
const LFO = await loadTypeScriptModule('src/lib/rb/fx/beat-fx-filter-kernel.ts');
const {
	exportFilterRatingPairs,
	assertPairsManifestShape,
	REQUIRED_COMPARISONS,
	WINNING_GLIDE,
	DEFAULT_SEED,
	REAL_PATH_DEPENDENCIES,
	VetoError,
	UnavailableError,
	resolveRealRenderer,
	_parseArgs
} = mod;

const SR = 8000;
const SECONDS = 3;
const QUANTUM = 128;
const LABELS = 'hz octave 5ms 20ms q2 q4 q8 12db 24db sine triangle down up'.split(' ');
const knobAt = (points, tMs) => {
	if (tMs <= points[0].t_ms) return points[0].value;
	for (let k = 1; k < points.length; k++) {
		const [p0, p1] = [points[k - 1], points[k]];
		if (tMs <= p1.t_ms)
			return p1.t_ms === p0.t_ms
				? p1.value
				: p0.value + ((p1.value - p0.value) * (tMs - p0.t_ms)) / (p1.t_ms - p0.t_ms);
	}
	return points[points.length - 1].value;
};
const targetHz = (direction, k) =>
	direction === 'hp'
		? 30 * (6300 / 30) ** Math.max(0, (k - 0.5) / 0.5)
		: 20000 * (130 / 20000) ** Math.max(0, (0.5 - k) / 0.5);
/** LFO FILTER corner per quantum: the kernel's own cutoff law at the request's shape, direction, division and tempo. */
const lfoCornerHz = (req, q) => {
	const periodSec = (req.lfo_beats * 60) / req.lfo_bpm;
	const reset = LFO.beatFxFilterResetPhase(periodSec);
	return LFO.beatFxFilterCutoffHz(
		(q * QUANTUM) / req.sampleRate / periodSec,
		req.lfo_shape,
		req.lfo_direction,
		reset
	);
};
/** Deterministic double: a one-pole glide on the corner in the asked domain (or the LFO corner), and a scalar gain from the corner position, Q and slope. */
const testDouble = ({ peakAt = null, nanAt = null, malform = null, holdFirst = false } = {}) => {
	let holdResolve = null;
	const holdPromise = holdFirst
		? new Promise((resolve) => {
				holdResolve = resolve;
			})
		: null;
	const state = { callLog: [], holdResolve, holdPromise };
	const double = {
		id: 'test-double:filter',
		realPath: false,
		callLog: state.callLog,
		releaseHold() {
			if (holdResolve) {
				holdResolve();
				holdResolve = null;
				state.holdPromise = null;
			}
		},
		async render(req) {
			const call = { callIndex: state.callLog.length, returned: false };
			state.callLog.push(call);
			if (holdPromise) {
				await holdPromise;
			}
			const n = req.input[0].length;
			const quanta = Math.ceil(n / QUANTUM);
			const cornerHz = new Float32Array(quanta);
			const alpha = 1 - Math.exp(-QUANTUM / req.sampleRate / (req.glide_ms / 1000));
			const toDomain = (hz) => (req.glide_domain === 'octave' ? Math.log2(hz) : hz);
			const fromDomain = (v) => (req.glide_domain === 'octave' ? 2 ** v : v);
			let glide = toDomain(targetHz(req.direction, knobAt(req.automation, 0)));
			for (let q = 0; q < quanta; q++) {
				glide +=
					alpha *
					(toDomain(
						targetHz(req.direction, knobAt(req.automation, ((q * QUANTUM) / req.sampleRate) * 1000))
					) -
						glide);
				cornerHz[q] = req.lfo_shape ? lfoCornerHz(req, q) : fromDomain(glide);
			}
			const travel = req.direction === 'hp' ? Math.log2(6300 / 30) : Math.log2(20000 / 130);
			const pos = (hz) => Math.abs(Math.log2(hz / (req.direction === 'hp' ? 30 : 20000))) / travel;
			const qGain = req.resonance_q_linear === null ? 1 : 1 + 0.02 * req.resonance_q_linear;
			const slopeGain = req.slope_db_oct === 24 ? 0.9 : 1;
			const channels = req.input.map((ch) =>
				Float32Array.from(
					ch,
					(x, i) => (x * qGain * slopeGain) / (1 + pos(cornerHz[Math.floor(i / QUANTUM)]))
				)
			);
			if (peakAt !== null) channels[0][peakAt] = 1.25;
			if (nanAt !== null) channels[1][nanAt] = Number.NaN;
			let result = { channels, cornerHz, quantum: QUANTUM };
			if (malform === 'channels') result = { channels: [channels[0]], cornerHz, quantum: QUANTUM };
			else if (malform === 'frames') {
				result = {
					channels: [channels[0], channels[1].subarray(0, n - 1)],
					cornerHz,
					quantum: QUANTUM
				};
			} else if (malform === 'quantum') result = { channels, cornerHz, quantum: 0 };
			else if (malform === 'corner-short') {
				result = {
					channels,
					cornerHz: cornerHz.subarray(0, Math.max(0, quanta - 1)),
					quantum: QUANTUM
				};
			} else if (malform === 'corner-nan') {
				const bad = Float32Array.from(cornerHz);
				bad[0] = Number.NaN;
				result = { channels, cornerHz: bad, quantum: QUANTUM };
			}
			call.returned = true;
			return result;
		}
	};
	return double;
};
const baseExportOpts = (rendererOverrides = {}) => ({
	seconds: SECONDS,
	sampleRate: SR,
	allowTestDouble: true,
	renderer: testDouble(rendererOverrides)
});
const inFlight = new Set();
const trackExport = (promise) => {
	inFlight.add(promise);
	return promise.finally(() => inFlight.delete(promise));
};
const waitForInFlightExports = () => Promise.allSettled([...inFlight]);
const spinUntil = (pred, maxTicks = 5000) =>
	new Promise((resolve, reject) => {
		let ticks = 0;
		const tick = () => {
			if (pred()) resolve();
			else if (++ticks >= maxTicks) reject(new Error('spinUntil timeout'));
			else setImmediate(tick);
		};
		setImmediate(tick);
	});
const dirs = [];
const freshDir = () => {
	const d = mkdtempSync(join(tmpdir(), 'filter-pairs-'));
	dirs.push(d);
	return d;
};
const releaseFixtureDirs = async () => {
	await waitForInFlightExports();
	for (const d of dirs) rmSync(d, { recursive: true, force: true });
	dirs.length = 0;
};
after(async () => {
	if (dirs.length) await releaseFixtureDirs();
});

let outDir;
let manifest;
before(async () => {
	outDir = join(freshDir(), 'round1');
	({ manifest } = await trackExport(exportFilterRatingPairs({ outDir, ...baseExportOpts() })));
});

const sorted = (p) => [p.meta.a_candidate, p.meta.b_candidate].sort().join(',');

test('manifest has the #9776 shape with 10 pairs covering every comparison, gesture and fixture', () => {
	assertPairsManifestShape(manifest);
	assert.equal(manifest.pairs.length, 10);
	assert.equal(manifest.family, 'filter');
	assert.equal(manifest.seed, DEFAULT_SEED);
	assert.deepEqual(manifest.renderer, { id: 'test-double:filter', real_path: false });
	for (const c of REQUIRED_COMPARISONS) {
		const want = [c.a, c.b].sort().join(',');
		assert.ok(
			manifest.pairs.some((p) => p.meta.axis === c.axis && sorted(p) === want),
			`${c.axis} ${want}`
		);
	}
	assert.equal(manifest.pairs.filter((p) => p.meta.axis === 'slope').length, 1);
	for (const direction of ['hp', 'lp']) {
		for (const ms of [150, 2000])
			assert.ok(
				manifest.pairs.some((p) => p.meta.direction === direction && p.meta.gesture_ms === ms),
				`${direction} ${ms}`
			);
	}
	assert.deepEqual(
		new Set(manifest.pairs.map((p) => p.meta.fixture)),
		new Set(['four-on-the-floor', 'vocal-led'])
	);
	assert.deepEqual(
		JSON.parse(readFileSync(join(outDir, 'pairs.json'), 'utf8')),
		JSON.parse(JSON.stringify(manifest))
	);
});

test('no repeats and no label leaks', () => {
	const n = manifest.pairs.length;
	assert.equal(new Set(manifest.pairs.map((p) => p.id)).size, n);
	assert.equal(new Set(manifest.pairs.flatMap((p) => [p.a, p.b])).size, 2 * n);
	assert.equal(new Set(manifest.pairs.map((p) => `${p.meta.key}`)).size, n);
	for (const p of manifest.pairs) {
		const visible = `${p.id} ${p.a} ${p.b} ${p.question}`.toLowerCase();
		for (const label of LABELS) assert.ok(!visible.includes(label), `${p.id} leaks ${label}`);
	}
});

test('every clip is a 16-bit stereo PCM WAV of the asked length and the two sides differ', () => {
	for (const p of manifest.pairs) {
		const [a, b] = [p.a, p.b].map((rel) => readFileSync(join(outDir, rel)));
		for (const wav of [a, b]) {
			assert.equal(wav.toString('ascii', 0, 4), 'RIFF');
			assert.equal(wav.toString('ascii', 8, 12), 'WAVE');
			assert.equal(wav.readUInt16LE(20), 1);
			assert.equal(wav.readUInt16LE(22), 2);
			assert.equal(wav.readUInt32LE(24), SR);
			assert.equal(wav.readUInt16LE(34), 16);
			assert.equal(wav.readUInt32LE(40), SR * SECONDS * 2 * 2);
		}
		assert.ok(!a.equals(b), `${p.id} a and b are byte-identical`);
	}
});

test('sides and order are seeded: same seed reproduces, another seed changes them', async () => {
	const dir = join(freshDir(), 'again');
	await trackExport(exportFilterRatingPairs({ outDir: dir, ...baseExportOpts() }));
	assert.ok(readFileSync(join(dir, 'pairs.json')).equals(readFileSync(join(outDir, 'pairs.json'))));
	const { manifest: other } = await trackExport(
		exportFilterRatingPairs({
			outDir: join(freshDir(), 'o'),
			...baseExportOpts(),
			seed: 12345
		})
	);
	const layout = (m) => m.pairs.map((p) => `${p.meta.key}:${p.meta.a_candidate}`).join(';');
	assert.notEqual(layout(other), layout(manifest));
	const canonicalOnA = manifest.pairs.filter(
		(p) =>
			['hz', '5ms', '12db', 'sine', 'down'].includes(p.meta.a_candidate) ||
			(p.meta.axis === 'resonance_q' && p.meta.a_candidate < p.meta.b_candidate)
	).length;
	assert.ok(canonicalOnA > 0 && canonicalOnA < 10, `unswapped count ${canonicalOnA}`);
});

test('every side carries finite proxies, HP residuals only on HP, and 5 ms glides further than 20 ms', () => {
	for (const p of manifest.pairs) {
		for (const side of ['a', 'b']) {
			const px = p.meta.proxies[side];
			for (const k of ['octave_span_at_gesture_end', 'corner_peak_db', 'integrated_rms_change_db'])
				assert.ok(Number.isFinite(px[k]), `${p.id} ${side} ${k}`);
			assert.ok(
				px.octave_span_fraction >= 0 && px.octave_span_fraction <= 1,
				`${p.id} ${side} fraction`
			);
			for (const f of [100, 160, 250]) {
				const v = px[`residual_${f}_hz_db`];
				if (p.meta.direction === 'hp') assert.ok(Number.isFinite(v), `${p.id} ${side} ${f}`);
				else assert.equal(v, null);
			}
		}
	}
	for (const p of manifest.pairs.filter((q) => q.meta.axis === 'glide_time')) {
		const side = (label) => (p.meta.a_candidate === label ? 'a' : 'b');
		assert.ok(
			p.meta.proxies[side('5ms')].octave_span_at_gesture_end >
				p.meta.proxies[side('20ms')].octave_span_at_gesture_end,
			p.meta.key
		);
	}
});

test('meta candidates match the requests after the side swap', () => {
	const expect = {
		hz: { glide_domain: 'hz' },
		octave: { glide_domain: 'octave' },
		'5ms': { glide_ms: 5 },
		'20ms': { glide_ms: 20 },
		q2: { resonance_q_linear: 2 },
		q4: { resonance_q_linear: 4 },
		q8: { resonance_q_linear: 8 },
		'12db': { slope_db_oct: 12 },
		'24db': { slope_db_oct: 24 },
		sine: { lfo_shape: 'sine', lfo_direction: 'down' },
		triangle: { lfo_shape: 'triangle', lfo_direction: 'down' },
		down: { lfo_shape: 'sine', lfo_direction: 'down' },
		up: { lfo_shape: 'sine', lfo_direction: 'up' }
	};
	for (const p of manifest.pairs) {
		for (const side of ['a', 'b']) {
			const req = p.meta[`${side}_request`];
			for (const [k, v] of Object.entries(expect[p.meta[`${side}_candidate`]]))
				assert.equal(req[k], v, `${p.id} ${side} ${k}`);
		}
	}
});

test('Q pairs render at the winning glide and its source is recorded', async () => {
	assert.equal(manifest.baseline.winning_glide_source, 'CFG');
	for (const p of manifest.pairs.filter((q) => q.meta.axis === 'resonance_q')) {
		for (const side of ['a', 'b']) {
			assert.equal(p.meta[`${side}_request`].glide_domain, WINNING_GLIDE.glide_domain);
			assert.equal(p.meta[`${side}_request`].glide_ms, WINNING_GLIDE.glide_ms);
		}
	}
	const { winningGlide } = _parseArgs(['--out', 'x', '--winning-glide', 'hz:20']);
	assert.deepEqual(winningGlide, { glide_domain: 'hz', glide_ms: 20 });
	const { manifest: m } = await trackExport(
		exportFilterRatingPairs({
			outDir: join(freshDir(), 'w'),
			...baseExportOpts(),
			winningGlide
		})
	);
	assert.equal(m.baseline.winning_glide_source, 'cli');
	for (const p of m.pairs.filter((q) => q.meta.axis === 'resonance_q'))
		assert.equal(p.meta.a_request.glide_ms, 20);
});

test('a peak veto stops the export naming the pair and sample, and writes nothing', async () => {
	const dir = join(freshDir(), 'peak');
	await assert.rejects(
		trackExport(exportFilterRatingPairs({ outDir: dir, ...baseExportOpts({ peakAt: 1234 }) })),
		(err) => {
			assert.ok(err instanceof VetoError);
			assert.equal(err.veto, 'peak');
			assert.match(
				err.message,
				/^peak veto: p\d [ab] \(.+\) clip sample 1234 measured 1\.2500 > 1\.0$/
			);
			return true;
		}
	);
	assert.equal(existsSync(dir), false);
});

test('a finite veto stops the export and writes nothing', async () => {
	const dir = join(freshDir(), 'nan');
	await assert.rejects(
		trackExport(exportFilterRatingPairs({ outDir: dir, ...baseExportOpts({ nanAt: 99 }) })),
		(err) => {
			assert.ok(err instanceof VetoError);
			assert.equal(err.veto, 'finite');
			return true;
		}
	);
	assert.equal(existsSync(dir), false);
});

test('assertPairsManifestShape rejects an eleventh pair, a leak, a missing HP residual and a non-finite proxy', () => {
	const copy = () => structuredClone(manifest);
	const eleven = copy();
	eleven.pairs.push({
		...structuredClone(eleven.pairs[0]),
		id: 'p11',
		a: 'clips/p11-a.wav',
		b: 'clips/p11-b.wav'
	});
	assert.throws(() => assertPairsManifestShape(eleven), /pairs must hold 1 to 10/);
	const leak = copy();
	leak.pairs[0].a = 'clips/octave-a.wav';
	assert.throws(() => assertPairsManifestShape(leak), /leaks candidate label octave/);
	const hp = copy();
	const hpPair = hp.pairs.find((p) => p.meta.direction === 'hp');
	delete hpPair.meta.proxies.a.residual_160_hz_db;
	assert.throws(() => assertPairsManifestShape(hp), /residual_160_hz_db/);
	const nan = copy();
	nan.pairs[0].meta.proxies.b.corner_peak_db = Number.NaN;
	assert.throws(() => assertPairsManifestShape(nan), /corner_peak_db/);
});

test('the CLI fails fast without --out', () => {
	const res = spawnSync(process.execPath, [fileURLToPath(SCRIPT_URL)], { encoding: 'utf8' });
	assert.equal(res.status, 1);
	assert.match(res.stderr, /^\[ERROR\].*--out/);
});

test('the CLI rejects invalid flag values with [ERROR] and writes nothing', () => {
	const out = join(freshDir(), 'bad');
	const res = spawnSync(
		process.execPath,
		[fileURLToPath(SCRIPT_URL), '--out', out, '--seed', 'abc'],
		{ encoding: 'utf8' }
	);
	assert.equal(res.status, 1);
	assert.match(res.stderr, /^\[ERROR\] invalid --seed/);
	assert.equal(existsSync(out), false);
});

test('the CLI rejects unknown flags with [ERROR] and writes nothing', () => {
	const out = join(freshDir(), 'bad');
	const res = spawnSync(
		process.execPath,
		[fileURLToPath(SCRIPT_URL), '--out', out, '--bogus', 'x'],
		{ encoding: 'utf8' }
	);
	assert.equal(res.status, 1);
	assert.match(res.stderr, /^\[ERROR\] unknown argument --bogus/);
	assert.equal(existsSync(out), false);
});

// Holds while any real-path dependency is absent from the checkout; rewrite it when the harness
// adapter lands in resolveRealRenderer.
test('the CLI reports UNAVAILABLE and writes nothing while the real render path is missing', () => {
	const repoRoot = fileURLToPath(new URL('../../../../../', import.meta.url));
	const missing = REAL_PATH_DEPENDENCIES.filter((d) => !existsSync(join(repoRoot, d.path)));
	assert.ok(missing.length > 0, 'every dependency is present: rewrite this test for the adapter');
	const out = join(freshDir(), 'round1');
	const res = spawnSync(process.execPath, [fileURLToPath(SCRIPT_URL), '--out', out], {
		encoding: 'utf8'
	});
	assert.equal(res.status, 2);
	assert.match(res.stderr, /^\[UNAVAILABLE\]/);
	for (const d of missing) assert.ok(res.stderr.includes(d.issue), d.issue);
	assert.equal(existsSync(out), false);
});

test('resolveRealRenderer names every missing dependency and stays UNAVAILABLE without an adapter', () => {
	assert.throws(
		() => resolveRealRenderer(freshDir()),
		(err) => {
			assert.ok(err instanceof UnavailableError);
			assert.deepEqual(
				err.missing.map((d) => d.issue),
				['#9774', '#9826', '#9828', '#9776']
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

test('a non-real or missing renderer is refused and nothing is written', async () => {
	const dir = join(freshDir(), 'refused');
	await assert.rejects(
		trackExport(
			exportFilterRatingPairs({
				outDir: dir,
				seconds: SECONDS,
				sampleRate: SR,
				renderer: testDouble()
			})
		),
		/not a real-path renderer/
	);
	assert.equal(existsSync(dir), false);
	await assert.rejects(
		trackExport(exportFilterRatingPairs({ outDir: dir, seconds: SECONDS, sampleRate: SR })),
		/renderer/
	);
});

test('a valid render that resolves after the export was replaced is rejected by the exporter and writes nothing', async () => {
	const renderer = testDouble({ holdFirst: true });
	const replaced = new AbortController();
	const dir = join(freshDir(), 'stale');
	const run = trackExport(
		exportFilterRatingPairs({
			outDir: dir,
			...baseExportOpts(),
			renderer,
			signal: replaced.signal
		})
	);
	await spinUntil(() => renderer.callLog.length > 0);
	replaced.abort();
	renderer.releaseHold();
	await assert.rejects(run, /resolved .+ after the export was replaced; stale result discarded/);
	assert.equal(renderer.callLog[0].returned, true, 'the held render returned a valid result');
	assert.equal(renderer.callLog.length, 1, 'no render starts after the replacement');
	assert.equal(existsSync(dir), false);
});

test('an export whose signal is never aborted renders past the hold and writes', async () => {
	const renderer = testDouble({ holdFirst: true });
	const live = new AbortController();
	const dir = join(freshDir(), 'live');
	const run = trackExport(
		exportFilterRatingPairs({ outDir: dir, ...baseExportOpts(), renderer, signal: live.signal })
	);
	await spinUntil(() => renderer.callLog.length > 0);
	renderer.releaseHold();
	const { manifest: m } = await run;
	assert.equal(m.pairs.length, 10);
	assert.ok(renderer.callLog.every((c) => c.returned));
	assert.equal(existsSync(join(dir, 'pairs.json')), true);
});

test('fixture release waits for in-flight exports before teardown can finish', async () => {
	const renderer = testDouble({ holdFirst: true });
	const root = freshDir();
	const dir = join(root, 'hold');
	const run = trackExport(
		exportFilterRatingPairs({
			outDir: dir,
			...baseExportOpts(),
			renderer
		})
	);
	await spinUntil(() => renderer.callLog.length > 0);
	assert.ok(existsSync(root));
	const waitDone = waitForInFlightExports();
	let settled = false;
	void waitDone.then(() => {
		settled = true;
	});
	await spinUntil(() => settled, 20).catch(() => {});
	assert.equal(settled, false);
	renderer.releaseHold();
	await run;
	await waitDone;
	assert.ok(existsSync(root));
	assert.equal(existsSync(dir), true);
});

test('malformed render results reject with malformed result and write nothing', async () => {
	for (const malform of ['channels', 'quantum']) {
		const dir = join(freshDir(), `malform-${malform}`);
		await assert.rejects(
			trackExport(exportFilterRatingPairs({ outDir: dir, ...baseExportOpts({ malform }) })),
			(err) => {
				assert.match(err.message, /malformed result for .+:/);
				return true;
			}
		);
		assert.equal(existsSync(dir), false);
	}
});

test('the LFO FILTER gains sine vs triangle and down vs up at one division, with the other clips proxies', () => {
	const lfoPairs = manifest.pairs.filter((p) =>
		['lfo_shape', 'lfo_direction'].includes(p.meta.axis)
	);
	assert.deepEqual(lfoPairs.map(sorted).sort(), ['down,up', 'sine,triangle']);
	const proxyKeys = Object.keys(
		manifest.pairs.find((p) => p.meta.axis === 'slope').meta.proxies.a
	).sort();
	for (const p of lfoPairs) {
		const [a, b] = [p.meta.a_request, p.meta.b_request];
		assert.equal(a.lfo_beats, b.lfo_beats, `${p.meta.key} division`);
		assert.equal(a.lfo_bpm, b.lfo_bpm, `${p.meta.key} tempo`);
		for (const side of ['a', 'b'])
			assert.deepEqual(Object.keys(p.meta.proxies[side]).sort(), proxyKeys);
		assert.notDeepEqual(p.meta.proxies.a, p.meta.proxies.b, `${p.meta.key} sides measure the same`);
	}
	assert.deepEqual(manifest.baseline.lfo_shipped, {
		lfo_shape: 'sine',
		lfo_direction: 'down',
		lfo_beats: 1
	});
	assertPairsManifestShape(manifest);
});
