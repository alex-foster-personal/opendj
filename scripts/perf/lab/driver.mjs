// perf-lab browser driver: drives /performance through the agent IPC and
// captures the Chromium-side evidence the analyzer attributes.
//
// One run writes into --out:
//   trace.pftrace        Perfetto proto trace (CDP Tracing, ReturnAsStream/proto):
//                        thread slices, in-trace V8 CPU samples, memory-infra
//                        dumps (per-allocator), blink.user_timing phase marks.
//   phases.json          phase windows [{name, t0_ms, t1_ms}] on the page clock
//                        plus the same windows as `perflab:<name>` user-timing
//                        measures inside the trace, so the analyzer can slice by
//                        phase without trusting two clocks to agree.
//   heap-<n>.heapsnapshot  V8 heap snapshots (leak scenario, MemLab 3-snapshot method).
//   latency.json         per-action latency rows (latency scenario).
//   controls.json        what was planted (controls), so the analyzer can score
//                        recovery: the harness must FIND what it planted.
//   driver.json          run metadata, deck load stages, errors.
//
// Controls exist because a profiler that reports nothing has not shown that
// nothing is there ((private path)): every run can plant a known
// CPU burner, a known retained buffer and a known leak, and the analyzer scores
// how much of each it recovers and attributes to the right place.
//
// Usage (see (private path)):
//   node scripts/perf/lab/driver.mjs --stack .tmp/perf-lab/stacks/<name>.json --ids a,b,c,d \
//     --out .tmp/perf-lab/<run> --scenario all --controls --engine webaudio
// --stack takes the URL, revision, Chromium and manifest from a `stack.py up`
// state file, and driver.json copies that manifest. --url with --commit still
// works, but such a capture has no manifest and compares as UNKNOWN; pass
// --engine-root and --vite-root (the pids the sampler roots at) for ledger add
// to check its sampler's scope.

import fs from 'node:fs';
import os from 'node:os';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { awaitHealthyDecks, decksNotReachingOutput, loadedDecksFaults, monitorAudio, unhealthyDecks, windowAudioProblems } from './audio-health.mjs';
import { browserEnv } from './browser-env.mjs';
import { installAudioProbe } from './audio-probe.mjs';
import { PRESS_INIT, press } from './press.mjs';
import { modelRowCollector } from './app-rows.mjs';
import { plantBuffer as plantPlantedBuffer, releasePlantedBuffer } from './controls.mjs';
import { audioOutputIdentity, captureFaults, graphicsIdentity, identityDrift, resolveChrome, stackRevision } from './capture-meta.mjs';
import { harnessMoved, harnessStamp } from './harness.mjs';
import { isTrackAsset, mediaChanges, mediaDigests } from './media.mjs';
import { nodeFloorRefusal } from './node-floor.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '../../..');
// refused before Playwright loads or anything is measured
const nodeRefusal = nodeFloorRefusal(process.versions.node, path.join(REPO, 'apps/webui/frontend/package.json'));
if (nodeRefusal) {
	console.error(`[FAIL] ${nodeRefusal}`);
	process.exit(2);
}
const requireFromFrontend = createRequire(path.join(REPO, 'apps/webui/frontend/package.json'));
// the measuring code and its Playwright, stamped before Playwright loads and
// verified again before driver.json is written (harness.mjs harnessMoved)
const startedAs = harnessStamp(requireFromFrontend);

let stackManifest = null;
let stackState = null;
// manifest.py re-measures the stack worktree's commit, dirty digest and
// lockfiles against the manifest `up` recorded; returns why they differ, or null.
function stackDrift(state, file) {
	const r = spawnSync(state.python, [path.join(HERE, 'manifest.py'), 'drift', file], { encoding: 'utf8' });
	if (r.status === 0) return null;
	if (r.status === 1) return r.stdout.trim();
	return `cannot measure stack drift (exit ${r.status ?? r.signal ?? r.error}): ${(r.stderr || '').trim()}`;
}
// stack.py show re-proves the stack still answers from the processes it
// launched (same create time, holding its ports); a dead stack whose port
// another server took would otherwise be profiled under this manifest.
function stackOwnership(state, file) {
	const name = path.basename(file).replace(/\.json$/, '');
	const r = spawnSync(state.python, [path.join(HERE, 'stack.py'), 'show', '--state', path.resolve(file)], { encoding: 'utf8' });
	if (r.status === 0) return null;
	return `stack ${name} is not the stack it recorded (stack.py show exit ${r.status ?? r.signal ?? r.error}): ${(r.stderr || '').trim()}`;
}
function parseArgs(argv) {
	const out = { scenario: 'all', engine: 'webaudio', controls: false, headless: false, phaseS: 15, leakCycles: 6, latencyReps: 100, width: 1728, height: 1080 };
	for (let i = 2; i < argv.length; i++) {
		const a = argv[i];
		const next = () => argv[++i];
		if (a === '--url') out.url = next();
		else if (a === '--stack') out.stack = next();
		else if (a === '--ids') out.ids = next().split(',').filter(Boolean);
		else if (a === '--out') out.out = next();
		else if (a === '--scenario') out.scenario = next();
		else if (a === '--engine') out.engine = next();
		else if (a === '--controls') out.controls = true;
		else if (a === '--headless') out.headless = true;
		else if (a === '--phase-s') out.phaseS = Number(next());
		else if (a === '--leak-cycles') out.leakCycles = Number(next());
		else if (a === '--latency-reps') out.latencyReps = Number(next());
		else if (a === '--chrome') out.chrome = next();
		else if (a === '--commit') out.commit = next();
		else if (a === '--engine-root') out.engineRoot = Number(next());
		else if (a === '--vite-root') out.viteRoot = Number(next());
		else if (a === '--viewport') { const [w, h] = next().split('x').map(Number); out.width = w; out.height = h; }
		else throw new Error(`unknown argument ${a}`);
	}
	// The stack's state names what it serves; a second source for any of it
	// could disagree with the manifest the capture copies.
	if (out.stack) {
		for (const k of ['url', 'commit', 'chrome']) if (out[k]) throw new Error(`--${k} comes from --stack; do not pass both`);
		for (const [k, flag] of [['engineRoot', 'engine-root'], ['viteRoot', 'vite-root']]) if (out[k] !== undefined) throw new Error(`--${flag} comes from --stack; do not pass both`);
		const state = JSON.parse(fs.readFileSync(out.stack, 'utf8'));
		if (!state.manifest || !state.browser) throw new Error(`${out.stack} has no manifest; bring the stack up with this stack.py`);
		out.url = state.url;
		out.commit = stackRevision(state, out.stack);
		out.chrome = state.browser;
		stackManifest = state.manifest;
		stackState = state;
		if (!state.python) throw new Error(`${out.stack} names no stack python; bring the stack up with this stack.py`);
		const drift = stackDrift(state, out.stack);
		if (drift) throw new Error(`${out.stack}: ${drift}; bring the stack down and up again`);
	}
	// --commit is the revision the profiled URL serves; ledger.py records it as the capture's revision.
	for (const k of ['url', 'ids', 'out', 'commit']) if (!out[k]) throw new Error(`--${k} is required`);
	for (const [k, flag] of [['engineRoot', 'engine-root'], ['viteRoot', 'vite-root']]) {
		if (out[k] !== undefined && !(Number.isInteger(out[k]) && out[k] > 0)) throw new Error(`--${flag} must be a pid, got ${out[k]}`);
	}
	if (out.ids.length < 1) throw new Error('--ids needs at least one stable_id');
	// a zero, negative or non-numeric count runs an empty workload that still
	// writes a complete-looking capture (three identical leak snapshots read OK)
	const counts = { '--leak-cycles': out.leakCycles, '--latency-reps': out.latencyReps, '--viewport width': out.width, '--viewport height': out.height };
	for (const [flag, v] of Object.entries(counts)) if (!Number.isInteger(v) || v < 1) throw new Error(`${flag} must be a positive integer, got ${v}`);
	if (!Number.isFinite(out.phaseS) || out.phaseS <= 0) throw new Error(`--phase-s must be a positive number of seconds, got ${out.phaseS}`);
	// a misspelled name would run nothing yet write a normal capture
	const SCENARIOS = ['steady', 'latency', 'leak'];
	const unknown = out.scenario === 'all' ? [] : out.scenario.split(',').filter((s) => !SCENARIOS.includes(s));
	if (unknown.length) throw new Error(`unknown --scenario ${unknown.join(', ')} (use all or a list of ${SCENARIOS.join(', ')})`);
	// a misspelled engine would silently run Web Audio while driver.json names it
	const ENGINES = ['webaudio', 'rust'];
	if (!ENGINES.includes(out.engine)) throw new Error(`unknown --engine ${out.engine} (use ${ENGINES.join(' or ')})`);
	const runs = (name) => out.scenario === 'all' || out.scenario.split(',').includes(name);
	// Rust runs in wall-clock mode and mixes outside the page, so the page's
	// destination probe hears nothing: steady's audio-health gate and latency's
	// audible times need page audio, so only leak can run on it.
	const pageAudio = ['steady', 'latency'].filter(runs);
	if (out.engine === 'rust' && pageAudio.length) {
		throw new Error(`--engine rust has no in-page audio output, which --scenario ${pageAudio.join(',')} measures; use --scenario leak`);
	}
	// The steady scenario publishes its loudest phase as four-playing and the
	// ledger keys four-deck KPIs on it, so it needs four tracks, never fewer.
	const steady = runs('steady');
	if (steady && out.ids.length < 4) throw new Error(`--scenario ${out.scenario} runs the four-deck steady phase and needs 4 --ids, got ${out.ids.length}`);
	return out;
}

const opts = parseArgs(process.argv);
fs.mkdirSync(opts.out, { recursive: true });
// analyze.py reads whatever capture artifacts it finds, so a run dir holding an
// earlier capture would mix two runs into one report. The sampler's own files
// (started first, into the same dir) are not ours and are allowed. driver.pid
// is ours: a stale one from a failed run would hand the sampler an old root
// whose surviving browser family it would charge to this capture. The sampler
// validates the pid too (live, driver.mjs, created after its launch), because
// this driver can write it before the backgrounded sampler first looks.
const OWNED = /^(driver\.pid|driver\.json|phases\.json|controls\.json|latency\.json|latency-traced\.json|trace\.pftrace|heap-\d+\.heapsnapshot|heapdiff\.json|native\.json|analysis\.json|report\.md|screen\.png)$/;
const stale = fs.readdirSync(opts.out).filter((f) => OWNED.test(f));
if (stale.length) {
	console.error(`[FAIL] ${opts.out} already holds a capture (${stale.slice(0, 4).join(', ')}); use a fresh --out`);
	process.exit(2);
}
// One capture per stack, claimed before anything touches it: a second capture
// would start on the first's warm engine, data copy and caches while copying
// the same cold-start manifest.
// The claim is an exclusive create (O_EXCL), so of two drivers started at once
// exactly one gets it; stack.py removes it when the stack goes down.
if (opts.stack) {
	const claim = `${opts.stack}.claim`;
	try {
		const fd = fs.openSync(claim, 'wx');
		fs.writeSync(fd, path.resolve(opts.out));
		fs.closeSync(fd);
	} catch (e) {
		if (e?.code !== 'EEXIST') throw e;
		const by = fs.readFileSync(claim, 'utf8') || 'another capture starting now';
		console.error(`[FAIL] ${opts.stack} was already captured into ${by}; bring the stack down and up again`);
		process.exit(2);
	}
	// after the claim, before anything is measured: still the stack's own processes
	const owner = stackOwnership(stackState, opts.stack);
	if (owner) {
		console.error(`[FAIL] ${opts.stack}: ${owner}; bring the stack down and up again`);
		process.exit(2);
	}
}
// Playwright loads only once the arguments, the run dir and the stack's claim
// and ownership have all passed, so each refusal above holds without it.
const { chromium } = requireFromFrontend('playwright');
// sampler.py --driver-dir adds this pid as a root (Chromium is our child).
fs.writeFileSync(path.join(opts.out, 'driver.pid'), String(process.pid));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// started_at and finished_at (UTC ISO) bracket the capture; host names the clock that
// took them, since ledger.py sampler_span compares them with sampler.json's epoch ticks
const meta = { started_at: new Date().toISOString(), host: os.hostname(), opts, manifest: stackManifest,
	// the process families the sampler must be rooted at (ledger.py sampler_scope)
	roots: { engine: stackState?.engine_pid ?? opts.engineRoot ?? null, vite: stackState?.vite_pid ?? opts.viteRoot ?? null, driver: process.pid }, harness: startedAs.harness, measurement_deps: startedAs.measurement_deps, errors: [], console_errors: [], loads: {}, scenarios: [] };
const writeJson = (name, value) => fs.writeFileSync(path.join(opts.out, name), JSON.stringify(value, null, 1));

// Categories: the union needed for thread attribution, JS sampling, frame
// production, Web Audio, GC and per-allocator memory. memory-infra is what
// splits a renderer's footprint into malloc / partition_alloc partitions /
// v8 / blink_gc / gpu / cc; without it "the tab uses 1.5 GB" cannot be split.
const TRACE_CATEGORIES = [
	'toplevel', 'toplevel.flow', 'sequence_manager', 'scheduler', 'renderer.scheduler',
	'blink', 'blink.user_timing', 'blink.animations', 'blink.gc', 'loading',
	'cc', 'gpu', 'viz', 'benchmark', 'input', 'latencyInfo', 'mojom', 'ipc',
	'v8', 'v8.execute', 'disabled-by-default-v8.cpu_profiler', 'disabled-by-default-v8.gc',
	'devtools.timeline', 'disabled-by-default-devtools.timeline', 'disabled-by-default-devtools.timeline.frame',
	'audio', 'webaudio', 'media',
	'disabled-by-default-memory-infra', 'memory'
];

// The frontend may pin a Playwright whose browser build is not installed (cloud
// containers ship one fixed build). Fall back to any installed Chromium rather
// than downloading one; --chrome always wins.
const chromePath = resolveChrome(chromium, opts.chrome);
const browser = await chromium.launch({ env: browserEnv(),
	headless: opts.headless,
	executablePath: chromePath,
	args: ['--autoplay-policy=no-user-gesture-required', '--enable-precise-memory-info']
});
// the GPU, driver and GL renderer this browser paints with: the ledger
// compares captures only within one (capture-meta.mjs graphicsIdentity)
const graphics = await graphicsIdentity(browser);
// the output device, sample rate and buffer latencies it plays through: the
// ledger compares captures only within one (capture-meta.mjs audioOutputIdentity)
const audioOutput = await audioOutputIdentity(browser);
const context = await browser.newContext({ viewport: { width: opts.width, height: opts.height } });

// Independent audible probe (audio-probe.mjs).
await context.addInitScript(installAudioProbe);

await context.addInitScript({ content: PRESS_INIT });

const page = await context.newPage();
page.on('pageerror', (e) => meta.errors.push(String(e)));
// every per-track asset a page loads (warm-up and measured page alike). Only
// recorded here: the digests are fetched before and after the measured phases,
// never alongside them, so the probe never charges its own work to a phase.
const assetRefs = new Set();
let mediaBefore = null;
const recordAsset = (r) => {
	if (r.method() !== 'GET') return;
	const u = new URL(r.url());
	if (isTrackAsset(u.pathname)) assetRefs.add(u.pathname + u.search);
};
page.on('request', recordAsset);
page.on('console', (m) => { if (m.type() === 'error') meta.console_errors.push(m.text().slice(0, 300)); });
const cdp = await context.newCDPSession(page);

// ---------------------------------------------------------------- tracing --
async function startTrace() {
	await cdp.send('Tracing.start', {
		transferMode: 'ReturnAsStream',
		streamFormat: 'proto',
		traceConfig: {
			recordMode: 'recordAsMuchAsPossible',
			traceBufferSizeInKb: 1_500_000,
			includedCategories: TRACE_CATEGORIES,
			memoryDumpConfig: { triggers: [{ mode: 'detailed', periodicIntervalMs: 5000 }] }
		}
	});
}
async function stopTrace(file) {
	const done = new Promise((resolve) => cdp.once('Tracing.tracingComplete', resolve));
	await cdp.send('Tracing.end');
	const { stream } = await done;
	const fd = fs.openSync(file, 'w');
	try {
		for (;;) {
			const chunk = await cdp.send('IO.read', { handle: stream, size: 8 << 20 });
			fs.writeSync(fd, chunk.base64Encoded ? Buffer.from(chunk.data, 'base64') : Buffer.from(chunk.data, 'binary'));
			if (chunk.eof) break;
		}
	} finally {
		fs.closeSync(fd);
		await cdp.send('IO.close', { handle: stream }).catch(() => {});
	}
	return fs.statSync(file).size;
}
async function memoryDump(label) {
	// An explicit detailed dump at a known moment, on top of the periodic ones.
	await mark(`dump:${label}`);
	const r = await cdp.send('Tracing.requestMemoryDump', { levelOfDetail: 'detailed' }).catch((e) => ({ success: false, error: String(e) }));
	if (!r.success) meta.errors.push(`memory dump ${label} failed: ${r.error ?? 'unknown'}`);
	return r;
}

// ----------------------------------------------------------------- phases --
const phases = [];
async function mark(name) { await page.evaluate((n) => performance.mark(`perflab:${n}`), name); }
async function phase(name, fn) {
	const t0 = await page.evaluate((n) => { performance.mark(`perflab:${n}:start`); return performance.now(); }, name);
	const wall0 = Date.now();
	await fn();
	const t1 = await page.evaluate((n) => { performance.measure(`perflab:${n}`, `perflab:${n}:start`); return performance.now(); }, name);
	phases.push({ name, t0_ms: t0, t1_ms: t1, wall_start: new Date(wall0).toISOString(), wall_s: (Date.now() - wall0) / 1000 });
	writeJson('phases.json', phases);
}

const ipc = (cmd) => page.evaluate(async (c) => {
	try { await window.musicDjToolsPerformance.dispatch(c); return { ok: true }; } catch (e) { return { ok: false, error: String(e) }; }
}, cmd);
async function loadDeck(deck, sid) {
	const r = await ipc({ type: 'load', deck, stable_id: sid });
	if (!r.ok) throw new Error(`load deck ${deck} ${sid}: ${r.error}`);
	await page.waitForFunction(({ deck, sid }) => {
		const d = window.musicDjToolsPerformance.query().decks[deck];
		return d?.stable_id === sid && d.transport_pending === false;
	}, { deck, sid }, { timeout: 90_000 });
	const stages = await page.evaluate((deck) => window.musicDjToolsPerformance.query().decks[deck]?.last_load_stages ?? null, deck);
	(meta.loads[deck] ??= []).push({ sid, stages });
}
async function setPlaying(deck, playing) {
	const r = await ipc({ type: 'play', deck, playing });
	if (!r.ok) throw new Error(`play deck ${deck}=${playing}: ${r.error}`);
}

// --------------------------------------------------------------- controls --
// Planted, named, sized. The analyzer looks for each by NAME and scores how much
// of the planted amount it recovers. The burner self-reports the CPU it really
// burned, so recovery is judged against what happened, not what was intended.
const controls = { planted: [] };
async function plantBurner(dutyMs = 8, periodMs = 50) {
	await page.evaluate(({ dutyMs, periodMs }) => {
		window.__perflabBurn = { burnedMs: 0, since: performance.now() };
		window.__perflabBurnTimer = setInterval(function perflabPlantedBurner() {
			const t = performance.now();
			let x = 0;
			while (performance.now() - t < dutyMs) x += Math.sqrt(x + 1);
			window.__perflabBurn.burnedMs += performance.now() - t;
			window.__perflabBurn.sink = x;
		}, periodMs);
	}, { dutyMs, periodMs });
}
async function stopBurner() {
	return page.evaluate(() => { clearInterval(window.__perflabBurnTimer); const b = window.__perflabBurn; return { burned_ms: b.burnedMs, window_ms: performance.now() - b.since }; });
}
async function plantBuffer(mib = 64) {
	await plantPlantedBuffer(page, mib);
	controls.planted.push({ kind: 'retained_arraybuffer', name: 'Float32Array (window.__perflabPlantedBuffer)', bytes: mib * 1048576 });
}

// -------------------------------------------------------------- scenarios --
async function waitReady() {
	await page.goto(`${opts.url}/performance${opts.engine === 'rust' ? '?engine=rust&engine_clock=wall' : '?engine=webaudio'}`);
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, { timeout: 180_000 });
	await page.mouse.move(4, opts.height - 4); // park the pointer off every hover target
	await sleep(4000);
}

// Warm-up, unmeasured and before the measured page exists: a throwaway browser
// context loads and briefly plays the measured decks, so every per-track asset
// a phase will request is known and hashed before the trace starts. It also
// warms the stack's own caches (the ANLZ cache, the OS file cache), so the
// measured deck loads are browser-cold but server-warm, by definition, and
// the baseline fetches below never warm anything a phase then measures.
async function warmUp() {
	const decks = Math.min(4, opts.ids.length);
	// every id a scenario can load (the leak cycles rotate through all of them), four decks at a time
	for (let i = 0; i < opts.ids.length; i += decks) await warmBatch(opts.ids.slice(i, i + decks));
	meta.warm_up = { ids: opts.ids, assets: assetRefs.size, caches: 'browser cold (own context), stack server-side caches warm' };
}
async function warmBatch(ids) {
	const decks = ids.length;
	const ctx = await browser.newContext({ viewport: { width: opts.width, height: opts.height } });
	try {
		const wp = await ctx.newPage();
		wp.on('request', recordAsset);
		await wp.goto(`${opts.url}/performance${opts.engine === 'rust' ? '?engine=rust&engine_clock=wall' : '?engine=webaudio'}`);
		await wp.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, { timeout: 180_000 });
		const send = async (cmd) => {
			const r = await wp.evaluate(async (c) => {
				try { await window.musicDjToolsPerformance.dispatch(c); return null; } catch (e) { return String(e); }
			}, cmd);
			if (r) throw new Error(`warm-up ${cmd.type} deck ${cmd.deck} refused: ${r}`);
		};
		for (let d = 1; d <= decks; d++) await send({ type: 'load', deck: d, stable_id: ids[d - 1] });
		let faults = [];
		for (const t = Date.now(); (faults = await loadedDecksFaults(wp, ids)).length && Date.now() - t < 90_000; ) await sleep(500);
		if (faults.length) throw new Error(`warm-up decks did not load: ${faults.join('; ')}`);
		for (let d = 1; d <= decks; d++) await send({ type: 'play', deck: d, playing: true });
		await sleep(3000);
		for (let d = 1; d <= decks; d++) await send({ type: 'play', deck: d, playing: false });
		await sleep(1000);
	} finally {
		await ctx.close();
	}
}

async function scenarioSteady() {
	await phase('idle-empty', () => sleep(opts.phaseS * 1000));
	await memoryDump('idle-empty');
	if (opts.controls) {
		await memoryDump('before-plant');
		await plantBuffer(64);
		await memoryDump('after-plant');
		// scored from those two dumps only; every later phase measures the app alone
		const held = await releasePlantedBuffer(page, cdp);
		if (held) {
			meta.invalid = held;
			throw new Error(held);
		}
		await plantBurner();
		await phase('control-burner', () => sleep(opts.phaseS * 1000));
		const b = await stopBurner();
		controls.planted.push({ kind: 'cpu_burner', name: 'perflabPlantedBurner', ...b });
	}
	await phase('load-4', async () => { for (let i = 0; i < Math.min(4, opts.ids.length); i++) await loadDeck(i + 1, opts.ids[i]); });
	await sleep(3000);
	await phase('loaded-stopped', () => sleep(opts.phaseS * 1000));
	await memoryDump('loaded-stopped');
	for (let d = 1; d <= Math.min(4, opts.ids.length); d++) await setPlaying(d, true);
	await sleep(3000);
	await requireHealthyAudio('four-playing start', true);
	// watched throughout: a dropout that recovers before the end check would
	// pass both endpoints while its missing DSP work read as a saving
	const monitor = await monitorAudio(page, Math.min(4, opts.ids.length), 1000);
	let samples;
	try {
		await phase('four-playing', () => sleep(opts.phaseS * 1000));
	} finally {
		samples = await monitor.stop(); // on every path: a throwing phase must not leave the watch running
	}
	const during = windowAudioProblems(samples, Math.max(3, Math.floor(opts.phaseS / 2)));
	if (during.length) {
		meta.invalid = `audio broken during four-playing: ${during.join('; ')}`;
		throw new Error(meta.invalid);
	}
	await requireHealthyAudio('four-playing end', false);
	await memoryDump('four-playing');
	for (let d = 1; d <= Math.min(4, opts.ids.length); d++) await setPlaying(d, false);
	meta.scenarios.push('steady');
}

// A failed worklet or decoder, a suspended context or an output that never
// presented idles the DSP and would read as a CPU saving, so decks that are not
// healthy (audio-health.mjs) at a measured phase's start (after settling) or
// end invalidate the capture.
async function requireHealthyAudio(label, settle) {
	const n = Math.min(4, opts.ids.length);
	let broken = settle ? await awaitHealthyDecks(page, n) : await unhealthyDecks(page, n);
	// each deck must reach the output on its own, not just the mix as a whole
	if (!broken.length) broken = await decksNotReachingOutput(page, n);
	if (broken.length) {
		meta.invalid = `audio broken at ${label}: ${broken.join('; ')}`;
		throw new Error(meta.invalid);
	}
}

// Latency: for each action, the press stamp, the dispatch resolve, the first
// query that reflects it, the next animation frame after that, and, for every
// action whose effect the independent probe can hear, when the output presents
// it. The Class A controls are measured against PLAYING audio: play (sound
// appears), fader cut (the level falls 20 dB) and open (sound again), and a
// low-EQ kill (the sub-bass band falls 12 dB against the mids). Both falls are
// measured against the median of the half second before the press, the onset
// of the change rather than the end of the app's parameter smoothing (a cut to
// true silence also times the smoothing's exponential tail: ~8 time constants).
// A press whose state reflects at once but whose audible effect lags would
// otherwise go unseen.
//
// idle_control arms the same level-drop and low-kill detectors with no command
// sent: a detector that fires there would fire on the music alone, so
// analyze.py voids those actions' audible numbers when it does. Each action a
// fall detector times has its OWN control on the SAME passage: the deck seeks
// to one point, the control listens, then it seeks back to that point and the
// action is pressed, so a natural drop later in the track cannot pass for the
// action's effect while the control heard an earlier, steadier passage. The
// control row names the action it validates (paired).
const LATENCY_STEPS = [
	{ action: 'play', seek: true },
	{ action: 'idle_control', paired: 'fader_cut', seek: true },
	{ action: 'fader_cut', seek: true },
	{ action: 'fader_open' },
	{ action: 'idle_control', paired: 'eq_low_kill', seek: true },
	{ action: 'eq_low_kill', seek: true },
	{ action: 'eq_low_restore' },
	{ action: 'pause' }
];
const FADER_OPEN = 1;
const EQ_UNITY = 0.5;
// Every rep starts from the same point in the track: 100 reps of 7 actions run
// past the end of most tracks, after which a cut fader no longer silences the
// output (live run Wed 14 Oct 2026: fader_cut unheard on reps 78-99 only), and
// one fixed passage makes the detectors' idle control speak for every rep.
const LATENCY_SEEK_MS = 30_000;
// every latency row, for captureFaults once the capture ends
const latencyRows = [];
// Two passes. The budget numbers (latency.json, LATENCY-01's p99s) come from an
// UNTRACED pass after the trace stops: continuous CPU profiling and memory-infra
// dumps stall renderer and audio work, and a traced p99 would time the profiler.
// A short pass inside the trace (latency-traced.json) only attributes main-thread
// work to each press (analyze.py reads its perflab:latency:* measures from the
// trace); none of its rows reach a KPI.
const LATENCY_TRACED_REPS = 10;
async function scenarioLatency({ reps, file, phaseName, budget }) {
	const deck = 1;
	if (!meta.loads[deck]) await loadDeck(deck, opts.ids[0]);
	// The rows are labeled as the sync-off, quantize-off path at an open fader and
	// unity low EQ, so a refused setup command would measure a different path.
	for (const cmd of [
		{ type: 'beat_sync', deck, enabled: false },
		{ type: 'quantize', deck, enabled: false, by_user: true },
		{ type: 'fader', deck, value: FADER_OPEN },
		{ type: 'eq', deck, band: 'low', value: EQ_UNITY }
	]) {
		const r = await ipc(cmd);
		if (!r.ok) throw new Error(`latency setup ${cmd.type} refused: ${r.error}`);
	}
	const rows = budget ? latencyRows : [];
	// The app's own staged model rows (press-to-schedule, scheduled offset,
	// device floor) for the same presses, for cross-checking the probe. Read
	// after every press: the app's ring holds only the last few (app-rows.mjs).
	const appModel = modelRowCollector();
	const perfRing = () => page.evaluate(() => window.__mdtPerfLog?.() ?? []);
	appModel.seed(await perfRing());
	// A processor that dies during a pass records processor_error, and the next
	// load (the leak cycles reload every deck) clears it before captureFaults
	// reads the final state, so the deck is checked after every rep, here.
	const processorFaults = async (when) => {
		const faults = await loadedDecksFaults(page, [meta.loads[deck].at(-1).sid]);
		if (!faults.length) return;
		meta.invalid ??= `${phaseName} ${when}: ${faults.join('; ')}`;
		throw new Error(`${phaseName} ${when}: deck not on a live processor: ${faults.join('; ')}`);
	};
	await phase(phaseName, async () => {
		for (let rep = 0; rep < reps; rep++) {
			for (const { action, paired = null, seek } of LATENCY_STEPS) {
				if (seek) {
					const r = await ipc({ type: 'seek', deck, position_ms: LATENCY_SEEK_MS });
					if (!r.ok) throw new Error(`latency rep ${rep} ${action}: seek refused: ${r.error}`);
					await page.waitForFunction((deck) => window.musicDjToolsPerformance.query().decks[deck]?.transport_pending === false, deck, { timeout: 10_000 });
				}
				// a real gesture on the page's control, timed from its input event (press.mjs);
				// the traced pass runs press.mjs's attribution mode
				const row = await press(page, { action, deck, FADER_OPEN, EQ_UNITY, attribution: !budget });
				row.rep = rep;
				row.paired = paired;
				row.seek_ms = seek ? LATENCY_SEEK_MS : null;
				rows.push(row);
				// the traced pass's rows reach no KPI or captureFaults, yet analyze.py
				// attributes its trace windows, so a refused press there voids it now
				if (!budget && row.press_error !== null) {
					meta.invalid ??= `${phaseName} rep ${rep} ${action}: press failed: ${row.press_error}`;
					throw new Error(meta.invalid);
				}
				await sleep(action === 'play' || action === 'fader_open' ? 700 : 250);
				appModel.take(await perfRing(), action);
			}
			await processorFaults(`rep ${rep}`);
		}
	});
	appModel.take(await perfRing());
	// every transport press the app accepted owes a model row (play and pause
	// both schedule one): L4's denominator per action, so a press that emitted
	// nothing counts against coverage and another action's rows cannot cover it
	const expected = {};
	for (const r of rows) {
		if ((r.action === 'play' || r.action === 'pause') && r.press_error === null) expected[r.action] = (expected[r.action] ?? 0) + 1;
	}
	writeJson(file, { traced: !budget, rows, app_model_rows: appModel.rows, app_model_expected_by_action: expected });
	if (budget) meta.scenarios.push('latency');
}

// Leak: MemLab's three-snapshot method on a real reload loop. Snapshot A after
// warm-up, B after the target cycles, C after more of the same; objects
// allocated between A and B that survive to C are the leak candidates. A
// planted leak (PerflabPlantedLeak, fixed count per cycle) proves the method
// can find a real one.
async function scenarioLeak() {
	const decks = Math.min(4, opts.ids.length);
	const alt = (cycle, deck) => opts.ids[(deck - 1 + cycle) % opts.ids.length];
	const snap = async (n) => {
		await page.evaluate(() => window.gc?.());
		await cdp.send('HeapProfiler.collectGarbage');
		const file = path.join(opts.out, `heap-${n}.heapsnapshot`);
		const fd = fs.openSync(file, 'w');
		const onChunk = (e) => fs.writeSync(fd, e.chunk);
		cdp.on('HeapProfiler.addHeapSnapshotChunk', onChunk);
		await cdp.send('HeapProfiler.takeHeapSnapshot', { reportProgress: false, captureNumericValue: false });
		cdp.off('HeapProfiler.addHeapSnapshotChunk', onChunk);
		fs.closeSync(fd);
		// No memoryDump here: the leak scenario runs after the trace has stopped,
		// and Tracing.requestMemoryDump needs an active trace.
	};
	await cdp.send('HeapProfiler.enable');
	// the first reload clears a processor_error an earlier phase left behind
	// decks 1..n in order, as loadedDecksFaults reads them; stop at the first never loaded
	const held = [];
	for (let d = 1; meta.loads[d]; d++) held.push(meta.loads[d].at(-1).sid);
	const before = await loadedDecksFaults(page, held);
	if (before.length) {
		meta.invalid ??= `before leak: ${before.join('; ')}`;
		throw new Error(`before leak: decks not on a live processor: ${before.join('; ')}`);
	}
	const cycle = async (c) => {
		for (let d = 1; d <= decks; d++) await loadDeck(d, alt(c, d));
		if (opts.controls) {
			await page.evaluate(() => {
				class PerflabPlantedLeak { constructor(i) { this.i = i; this.payload = new Array(16).fill(i); } }
				window.__perflabLeak ??= [];
				for (let i = 0; i < 20000; i++) window.__perflabLeak.push(new PerflabPlantedLeak(i));
			});
		}
		await sleep(1500);
		// every cycle, before the next load can clear a processor_error: a deck
		// whose processor died is not churning the loads the snapshots measure
		const faults = await loadedDecksFaults(page, [...Array(decks).keys()].map((i) => alt(c, i + 1)));
		if (faults.length) {
			meta.invalid ??= `leak cycle ${c}: ${faults.join('; ')}`;
			throw new Error(`leak cycle ${c}: decks not loaded on a live processor: ${faults.join('; ')}`);
		}
	};
	await phase('leak', async () => {
		await cycle(0);
		await snap('A');
		for (let c = 1; c <= opts.leakCycles; c++) await cycle(c);
		await snap('B');
		for (let c = opts.leakCycles + 1; c <= opts.leakCycles * 2; c++) await cycle(c);
		await snap('C');
	});
	if (opts.controls) controls.planted.push({ kind: 'js_leak', name: 'PerflabPlantedLeak', count_per_cycle: 20000, cycles_between_A_and_C: opts.leakCycles * 2 });
	meta.scenarios.push('leak');
}

try {
	await warmUp();
	await waitReady();
	// the inputs as they are before any measured phase: every fixture id's audio
	// and /anlz plus each asset the warm-up and this page requested. The end of
	// the run hashes them again; a change, or an asset first requested inside a
	// measured phase (it has no baseline), voids the capture (media.mjs)
	mediaBefore = await mediaDigests(opts.url, opts.ids, [...assetRefs]);
	meta.page = await page.evaluate(() => ({ ua: navigator.userAgent, dpr: devicePixelRatio, origin: location.origin, cross_origin_isolated: self.crossOriginIsolated }));
	await startTrace();
	const want = (s) => opts.scenario === 'all' || opts.scenario.split(',').includes(s);
	if (want('steady')) await scenarioSteady();
	if (want('latency')) await scenarioLatency({ reps: Math.min(LATENCY_TRACED_REPS, opts.latencyReps), file: 'latency-traced.json', phaseName: 'latency-traced', budget: false });
	meta.trace_bytes = await stopTrace(path.join(opts.out, 'trace.pftrace'));
	if (want('latency')) await scenarioLatency({ reps: opts.latencyReps, file: 'latency.json', phaseName: 'latency', budget: true });
	// Heap snapshots are huge trace-independent captures; take them untraced so
	// they do not dominate the trace buffer.
	if (want('leak')) await scenarioLeak();
	meta.final_query = await page.evaluate(() => {
		const q = window.musicDjToolsPerformance.query();
		return Object.fromEntries([1, 2, 3, 4].map((d) => [d, q.decks[d] && { stable_id: q.decks[d].stable_id, playing: q.decks[d].playing, processor_error: q.decks[d].processor_error }]));
	});
	for (const why of captureFaults({ latencyRows, finalQuery: meta.final_query })) {
		meta.errors.push(`capture: ${why}`);
		meta.invalid ??= why;
	}
	await page.screenshot({ path: path.join(opts.out, 'screen.png') });
} catch (e) {
	meta.errors.push(`FATAL ${e?.stack ?? e}`);
	process.exitCode = 2;
} finally {
	meta.browser_version = browser.version();
	meta.graphics = graphics;
	meta.audio_output = audioOutput;
	// read again once the measuring is over (capture-meta.mjs identityDrift, as
	// abwin.mjs does for each A/B browser): a GPU process that fell back to
	// another backend, or an output that switched, during the capture has no
	// one identity, and neither does one that can no longer be read
	const launchDrift = await identityDrift(browser, { graphics, audio_output: audioOutput });
	if (launchDrift) {
		meta.errors.push(`capture: ${launchDrift}`);
		meta.invalid ??= launchDrift;
	}
	meta.browser_executable = chromePath;
	// the manifest's copy is honest only if this is the build it names
	if (stackManifest && meta.browser_version !== stackManifest.browser_version) {
		const why = `launched Chromium ${meta.browser_version}, but the stack manifest names ${stackManifest.browser_version}`;
		meta.errors.push(`capture: ${why}`);
		meta.invalid ??= why;
	}
	// the worktree is served live, so an edit during the capture is measured too
	const drift = stackState && stackDrift(stackState, opts.stack);
	if (drift) {
		meta.errors.push(`capture: ${drift}`);
		meta.invalid ??= drift;
	}
	// and it is still served by the processes the stack launched
	const owner = stackState && stackOwnership(stackState, opts.stack);
	if (owner) {
		meta.errors.push(`capture: ${owner}`);
		meta.invalid ??= owner;
	}
	// the inputs each fixture id resolved to, wherever they live (media.mjs)
	const media = await mediaDigests(opts.url, opts.ids, [...assetRefs]);
	meta.media = media.digests;
	const before = mediaBefore ?? { digests: {}, errors: ['inputs were not hashed before the measured phases'] };
	meta.media_before = before.digests;
	const changed = mediaChanges(before.digests, media.digests).map((ref) => `${ref} changed during the capture`);
	// every asset measured needs a baseline; one first requested inside a phase has none
	const unbased = Object.keys(media.digests).filter((ref) => !(ref in before.digests)).map((ref) => `${ref} was first requested inside a measured phase, with no baseline digest`);
	for (const why of [...before.errors, ...media.errors, ...changed, ...unbased]) {
		meta.errors.push(`capture: ${why}`);
		meta.invalid ??= why;
	}
	// a harness that moved while it measured has no one identity: none is
	// recorded (the ledger refuses a driver.json with no harness) and the capture is invalid
	const harnessFault = harnessMoved(startedAs, harnessStamp(requireFromFrontend));
	if (harnessFault) {
		Object.assign(meta, { harness: null, measurement_deps: null, harness_fault: harnessFault });
		meta.errors.push(`capture: ${harnessFault}`);
		meta.invalid ??= harnessFault;
		process.exitCode = 3;
	}
	meta.finished_at = new Date().toISOString();
	writeJson('controls.json', controls);
	writeJson('phases.json', phases);
	writeJson('driver.json', meta);
	await browser.close();
}
