// perf-lab A/B windows: measure two running Open DJ stacks (A = base, B = change)
// in alternating, untraced windows, so a fix's saving is a difference of two
// numbers taken minutes apart on the same box, not two runs an hour apart.
//
// Why untraced: Chromium tracing with memory-infra roughly doubles idle renderer
// CPU on this box (46.6% untraced vs ~100% traced, 13 Oct 2026), which would
// swamp a 2-point saving. CPU comes from /proc ticks of the browser's own process
// family; a short traced window AFTER each CPU window adds frame and paint rates.
//
// Only one browser runs at a time. Each rep opens a fresh browser per side, in
// ABBA order across reps, and walks the same states:
//   idle-empty -> load-4 (stage timings) -> loaded-stopped -> four-playing
// Per state: renderer/gpu/browser/engine CPU (% of one core), renderer PSS and
// USS, JS heap used after a forced GC, and (traced 5 s) BeginMainFrame/s,
// main-thread paints/s, rAF callbacks/s.
//
// Usage:
//   node scripts/perf/lab/abwin.mjs --a http://127.0.0.1:9401 --b http://127.0.0.1:9402 \
//     --a-engine-pid 123 --b-engine-pid 456 --ids s1,s2,s3,s4 --out DIR \
//     [--reps 3] [--win 10] [--states idle-empty,loaded-stopped,four-playing] [--chrome PATH]
// Writes DIR/ab.json (raw rows + per-side mean/sd/n + B-A deltas).
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { explicitChrome, identityChanges, identityDrift, launchIdentity, playwrightChromium } from './capture-meta.mjs';
import { awaitStableFamily, cpuWindow } from './procwin.mjs';
import { awaitHealthyDecks, decksNotReachingOutput, loadedDecksFaults, monitorAudio, unhealthyDecks, windowAudioProblems } from './audio-health.mjs';
import { browserEnv } from './browser-env.mjs';
import { installAudioProbe } from './audio-probe.mjs';
import { compareLoads, summarizeStates } from './abstats.mjs';
import { isTrackAsset, mediaChanges, mediaDigests, mediaSidesDiffer } from './media.mjs';
import { nodeFloorRefusal } from './node-floor.mjs';
import { harnessMoved, harnessStamp } from './harness.mjs';

// how often the four-playing window's audio is checked while it runs
const AUDIO_CHECK_MS = 1000;
const HERE = path.dirname(fileURLToPath(import.meta.url));
// refused before Playwright loads or any window runs
const nodeRefusal = nodeFloorRefusal(process.versions.node, path.resolve(HERE, '../../../apps/webui/frontend/package.json'));
if (nodeRefusal) {
	console.error(`[FAIL] ${nodeRefusal}`);
	process.exit(2);
}
// playwright from this harness checkout's frontend, whatever the working directory
const require = createRequire(path.resolve(HERE, '../../../apps/webui/frontend/package.json'));
// stamped before Playwright loads, verified again before the final ab.json
const startedAs = harnessStamp(require);

function parseArgs(argv) {
	const o = { reps: 3, win: 10, states: ['idle-empty', 'loaded-stopped', 'four-playing'], ids: [], width: 1728, height: 1080 };
	for (let i = 2; i < argv.length; i++) {
		const a = argv[i], next = () => argv[++i];
		if (a === '--a') o.a = next();
		else if (a === '--b') o.b = next();
		else if (a === '--a-engine-pid') o.aPid = next();
		else if (a === '--b-engine-pid') o.bPid = next();
		else if (a === '--ids') o.ids = next().split(',').filter(Boolean);
		else if (a === '--out') o.out = next();
		else if (a === '--reps') o.reps = Number(next());
		else if (a === '--win') o.win = Number(next());
		else if (a === '--states') o.states = next().split(',');
		else if (a === '--chrome') o.chrome = next();
		else if (a === '--label') o.label = next();
		else throw new Error(`unknown arg ${a}`);
	}
	if (!o.a || !o.b || !o.out) throw new Error('--a, --b and --out are required');
	// one rep cannot estimate noise, so no comparison could resolve: refuse it up front
	if (!Number.isInteger(o.reps) || o.reps < 2) throw new Error(`--reps must be an integer >= 2 (one sample has no noise estimate), got ${o.reps}`);
	// an empty or negative window measures nothing yet still yields rows
	if (!Number.isFinite(o.win) || o.win <= 0) throw new Error(`--win must be a positive number of seconds, got ${o.win}`);
	// a misspelled state would measure nothing yet leave an empty, complete-looking summary
	const KNOWN_STATES = ['idle-empty', 'loaded-stopped', 'four-playing'];
	const unknown = o.states.filter((s) => !KNOWN_STATES.includes(s));
	if (!o.states.length || unknown.length) throw new Error(`--states: unknown ${unknown.join(',') || '(none given)'}; known: ${KNOWN_STATES.join(',')}`);
	// every state past idle-empty loads decks, and four-playing is keyed as four decks
	if (o.states.some((s) => s !== 'idle-empty') && o.ids.length < 4) throw new Error(`--states ${o.states.join(',')} loads four decks and needs 4 --ids, got ${o.ids.length}`);
	return o;
}
const opts = parseArgs(process.argv);
// Playwright loads only once the arguments passed, so each refusal holds without it
const { chromium } = require('playwright');
fs.mkdirSync(opts.out, { recursive: true });
// an earlier run's ab.json must not survive a failure before this run's first write
fs.rmSync(path.join(opts.out, 'ab.json'), { force: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function chromePath() {
	if (opts.chrome) return explicitChrome(opts.chrome);
	const p = chromium.executablePath();
	if (p && fs.existsSync(p)) return p;
	const found = playwrightChromium();
	if (found) return found;
	throw new Error('no Chromium found: pass --chrome');
}
const CHROME = chromePath();

// ------------------------------------------------------------ trace window --
async function frameWindow(cdp, secs) {
	const events = [];
	const onData = (e) => events.push(...e.value);
	cdp.on('Tracing.dataCollected', onData);
	const done = new Promise((r) => cdp.once('Tracing.tracingComplete', r));
	await cdp.send('Tracing.start', { traceConfig: { includedCategories: ['devtools.timeline', 'disabled-by-default-devtools.timeline', 'cc', 'blink'] }, transferMode: 'ReportEvents' });
	await sleep(secs * 1000);
	await cdp.send('Tracing.end');
	await done;
	cdp.off('Tracing.dataCollected', onData);
	const tn = {};
	for (const e of events) if (e.ph === 'M' && e.name === 'thread_name') tn[`${e.pid}/${e.tid}`] = e.args.name;
	let bmf = 0, paints = 0, raf = 0, mainMs = 0;
	for (const e of events) {
		if (tn[`${e.pid}/${e.tid}`] !== 'CrRendererMain' || e.ph !== 'X') continue;
		if (e.name === 'ProxyMain::BeginMainFrame' || e.name === 'ThreadProxy::BeginMainFrame') bmf++;
		else if (e.name === 'Paint') paints++;
		else if (e.name === 'FireAnimationFrame') raf++;
		if (e.name === 'ThreadControllerImpl::RunTask' || e.name === 'RunTask') mainMs += (e.dur ?? 0) / 1000;
	}
	const r = (n) => +(n / secs).toFixed(1);
	return { begin_main_frames_per_s: r(bmf), main_paints_per_s: r(paints), raf_callbacks_per_s: r(raf), main_task_pct: +((mainMs / secs) / 10).toFixed(1) };
}

// ----------------------------------------------------------------- one side --
async function runSide(side, url, enginePid, rep) {
	const browser = await chromium.launch({ env: browserEnv(), headless: false, executablePath: CHROME, args: ['--autoplay-policy=no-user-gesture-required', '--enable-precise-memory-info'] });
	const build = browser.version();
	// what this launch paints and plays through, before any measured page
	const identity = await stampLaunch(browser, side, rep);
	// Browser.process() exists only on a BrowserServer; this node process is the
	// browser's parent, so its descendants are exactly this browser's family.
	const bpid = process.pid;
	const ctx = await browser.newContext({ viewport: { width: opts.width, height: opts.height } });
	// what reaches the destination, so a silent side cannot pass as healthy
	await ctx.addInitScript(installAudioProbe);
	const page = await ctx.newPage();
	const errors = [];
	page.on('pageerror', (e) => errors.push(String(e).slice(0, 300)));
	page.on('request', (req) => {
		const u = new URL(req.url());
		if (isTrackAsset(u.pathname)) assetRefs[side].add(u.pathname + u.search);
	});
	const cdp = await ctx.newCDPSession(page);
	const rows = [];
	const heap = async () => {
		await cdp.send('HeapProfiler.collectGarbage');
		const h = await cdp.send('Runtime.getHeapUsage');
		return +(h.usedSize / 1048576).toFixed(1);
	};
	// watchDecks: decks to keep under audio watch for the whole window; returns
	// why the window's audio cannot be trusted ([] when it can)
	const measure = async (state, watchDecks = 0) => {
		await page.mouse.move(4, opts.height - 4);
		await sleep(1500);
		if (!(await awaitStableFamily(bpid, enginePid))) errors.push(`${side} rep ${rep} ${state}: process family still churning after 20 s; the window may be voided`);
		const monitor = watchDecks ? await monitorAudio(page, watchDecks, AUDIO_CHECK_MS) : null;
		let cpu, frames, samples;
		try {
			cpu = await cpuWindow(bpid, enginePid, opts.win);
			frames = await frameWindow(cdp, 5);
		} finally {
			// stopped on every path: a window that throws must not leave the watch running
			samples = monitor ? await monitor.stop() : null;
		}
		const audio = samples ? windowAudioProblems(samples, Math.max(3, Math.floor((opts.win * 1000) / AUDIO_CHECK_MS / 2))) : [];
		rows.push({ side, rep, state, ...cpu, ...frames, js_heap_mb: await heap() });
		return audio;
	};
	const ipc = (cmd) => page.evaluate(async (c) => { await window.musicDjToolsPerformance.dispatch(c); }, cmd);
	const loads = [];
	try {
		// A cold vite re-optimizes deps on first request and the first page load
		// can fail its dynamic imports; one reload is the normal recovery.
		for (let attempt = 0; ; attempt++) {
			await page.goto(`${url}/performance?engine=webaudio`);
			try {
				await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, { timeout: attempt ? 180_000 : 60_000 });
				break;
			} catch (e) {
				if (attempt) throw e;
				errors.length = 0;
			}
		}
		await sleep(6000);
		if (opts.states.includes('idle-empty')) await measure('idle-empty');
		const n = Math.min(4, opts.ids.length);
		if (n && opts.states.some((s) => s !== 'idle-empty')) {
			for (let d = 1; d <= n; d++) {
				const t0 = Date.now();
				await ipc({ type: 'load', deck: d, stable_id: opts.ids[d - 1] });
				await page.waitForFunction(({ d, s }) => { const q = window.musicDjToolsPerformance.query().decks[d]; return q?.stable_id === s && q.transport_pending === false; }, { d, s: opts.ids[d - 1] }, { timeout: 90_000 });
				const stages = await page.evaluate((d) => window.musicDjToolsPerformance.query().decks[d]?.last_load_stages ?? null, d);
				loads.push({ side, rep, deck: d, wall_ms: Date.now() - t0, stages });
			}
			await sleep(3000);
			if (opts.states.includes('loaded-stopped')) {
				// A dead processor clears its deck and holds processor_error until
				// the next load, and no load happens inside the window, so a check
				// on each side of it sees any failure during it: a side whose decks
				// lost their tracks would idle and read as a CPU saving.
				const expected = opts.ids.slice(0, n);
				const before = await loadedDecksFaults(page, expected);
				if (before.length) errors.push(`loaded-stopped not measured, decks not loaded: ${before.join('; ')}`);
				else {
					await measure('loaded-stopped');
					const after = await loadedDecksFaults(page, expected);
					if (after.length) {
						rows.pop();
						errors.push(`loaded-stopped row dropped, decks lost their load during the window: ${after.join('; ')}`);
					}
				}
			}
			if (opts.states.includes('four-playing')) {
				for (let d = 1; d <= n; d++) await ipc({ type: 'play', deck: d, playing: true });
				await sleep(3000);
				// A side whose DSP failed, whose context is suspended or whose output
				// never presented would idle and read as a CPU saving, so every deck
				// must be healthy (audio-health.mjs) once settled before the window
				// and still healthy after it; a failure inside it voids the row.
				const brokenDecks = async () => {
					const why = await unhealthyDecks(page, n);
					return why.length ? why : decksNotReachingOutput(page, n);
				};
				let before = await awaitHealthyDecks(page, n);
				if (!before.length) before = await decksNotReachingOutput(page, n);
				if (before.length) errors.push(`four-playing not measured, audio broken: ${before.join('; ')}`);
				else {
					const during = await measure('four-playing', n);
					const after = [...during, ...(await brokenDecks())];
					if (after.length) {
						rows.pop();
						errors.push(`four-playing row dropped, audio broke during the window: ${after.join('; ')}`);
					}
				}
			}
		}
	} catch (e) {
		errors.push(`FATAL ${String(e?.stack ?? e).slice(0, 600)}`);
	} finally {
		await restampLaunch(browser, side, rep, identity);
		await browser.close();
	}
	return { rows, loads, errors, build, identity };
}

const result = { label: opts.label ?? null, started_at: new Date().toISOString(), opts: { ...opts }, chrome: CHROME, browsers: [], rows: [], loads: [], errors: [], media: {}, media_faults: [], identity: null, identity_faults: [],
	// the measuring code and the node and Playwright it ran on (ab.py checks them against its own hash)
	harness: startedAs.harness, measurement_deps: startedAs.measurement_deps, harness_verified: false, harness_fault: null };
// Every browser launch, warm-ups included, reads its graphics stack and audio
// output (capture-meta.mjs launchIdentity) before any page exists. The first
// launch is the reference: a side or rep that fell back to SwiftShader, or
// played through another output, measures other work, so any launch that
// differs from it, or whose identity cannot be read, is an identity fault and
// ab.py makes the A/B UNKNOWN.
async function stampLaunch(browser, side, rep) {
	let identity;
	try {
		identity = await launchIdentity(browser);
	} catch (e) {
		result.identity_faults.push(`${side} ${rep}: browser identity unreadable: ${String(e?.message ?? e).slice(0, 200)}`);
		return null;
	}
	// fail closed: a launch whose concrete output device is unknown has no
	// output identity to hold the others to (capture-meta.mjs defaultOutputDevice)
	if (!identity.audio_output?.device) result.identity_faults.push(`${side} ${rep}: audio output device undeterminable: ${identity.audio_output?.device_unknown ?? 'not recorded'}`);
	if (!result.identity) result.identity = { launch: `${side} ${rep}`, graphics: identity.graphics, audio_output: identity.audio_output };
	else {
		const changed = identityChanges(result.identity, identity);
		if (changed.length) result.identity_faults.push(`${side} ${rep}: differs from ${result.identity.launch} in ${changed.join(', ')}`);
	}
	return identity;
}
// Each browser's identity is read again after its windows, before it closes
// (capture-meta.mjs identityDrift, as driver.mjs does): a GPU process that
// fell back to software mid-launch would leave every launch stamp agreeing
// while its rows hold software-rendered work. A change, or a re-read that
// fails, is an identity fault. A launch whose stamp already failed is faulted.
async function restampLaunch(browser, side, rep, stamped) {
	if (!stamped) return;
	const why = await identityDrift(browser, stamped);
	if (why) result.identity_faults.push(`${side} ${rep}: ${why}`);
}
// The inputs each side measured (media.mjs): the audio and /anlz of every
// fixture id plus every per-track asset its pages loaded, hashed as the stack
// serves them before the first window and after each side's windows, between
// windows only. An input outside the fixture data dir replaced during the run
// would otherwise be measured under an unchanged manifest; ab.py makes any
// change, or an input it cannot hash, UNKNOWN.
const assetRefs = { A: new Set(), B: new Set() };
// The warm-up loads and plays the same decks so every asset a window requests
// has a baseline before the first window; one first seen in a window has no
// earlier identity, so it is a fault too, never a silent pass.
async function checkMedia(side, url, when, baseline = false) {
	const { digests, errors } = await mediaDigests(url, opts.ids, [...assetRefs[side]]);
	const before = result.media[side] ?? {};
	for (const why of errors) result.media_faults.push(`${side} ${when}: ${why}`);
	for (const ref of mediaChanges(before, digests)) result.media_faults.push(`${side} ${when}: ${ref} changed during the run`);
	if (!baseline) for (const ref of Object.keys(digests)) if (!(ref in before)) result.media_faults.push(`${side} ${when}: ${ref} was first requested inside a window, with no baseline`);
	result.media[side] = { ...before, ...digests };
}
// Warm-up: one untimed page load per side, so vite's first-request transform
// and dependency optimization never land inside a measured window. It loads
// and briefly plays the measured decks too, recording every per-track asset
// they request, so each has a baseline digest before the first window.
for (const [side, url] of [['A', opts.a], ['B', opts.b]]) {
	const browser = await chromium.launch({ env: browserEnv(), headless: false, executablePath: CHROME, args: ['--autoplay-policy=no-user-gesture-required'] });
	const warmIdentity = await stampLaunch(browser, side, 'warm-up');
	result.browsers.push({ side, rep: 'warm-up', version: browser.version(), identity: warmIdentity });
	try {
		const page = await browser.newPage();
		page.on('request', (req) => {
			const u = new URL(req.url());
			if (isTrackAsset(u.pathname)) assetRefs[side].add(u.pathname + u.search);
		});
		await page.goto(`${url}/performance?engine=webaudio`);
		await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, { timeout: 240_000 }).catch(async () => {
			await page.reload();
			await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, { timeout: 240_000 });
		});
		const decks = Math.min(4, opts.ids.length);
		const ipc = (cmd) => page.evaluate(async (c) => { await window.musicDjToolsPerformance.dispatch(c); }, cmd);
		for (let d = 1; d <= decks; d++) await ipc({ type: 'load', deck: d, stable_id: opts.ids[d - 1] });
		for (const t = Date.now(); (await loadedDecksFaults(page, opts.ids.slice(0, decks))).length && Date.now() - t < 60_000; ) await sleep(500);
		for (let d = 1; d <= decks; d++) await ipc({ type: 'play', deck: d, playing: true });
		await sleep(3000);
		for (let d = 1; d <= decks; d++) await ipc({ type: 'play', deck: d, playing: false });
		await sleep(1000);
	} catch (e) {
		result.errors.push(`warm-up ${side}: ${String(e).slice(0, 300)}`);
	} finally {
		await restampLaunch(browser, side, 'warm-up', warmIdentity);
		await browser.close();
	}
}
// Both sides are hashed on every asset either warm-up requested, so their
// baselines share one key set, and the two baselines must then be identical:
// the same fixture ids resolving to different bytes on A and B would be two
// workloads, and neither side's later digests could ever show it.
const warmed = [...assetRefs.A, ...assetRefs.B];
for (const side of ['A', 'B']) for (const ref of warmed) assetRefs[side].add(ref);
for (const [side, url] of [['A', opts.a], ['B', opts.b]]) await checkMedia(side, url, 'before the windows', true);
for (const why of mediaSidesDiffer(result.media.A ?? {}, result.media.B ?? {})) result.media_faults.push(`before the windows: ${why}`);
for (let rep = 0; rep < opts.reps; rep++) {
	const order = rep % 2 ? [['B', opts.b, opts.bPid], ['A', opts.a, opts.aPid]] : [['A', opts.a, opts.aPid], ['B', opts.b, opts.bPid]];
	for (const [side, url, pid] of order) {
		const r = await runSide(side, url, pid, rep);
		result.browsers.push({ side, rep, version: r.build ?? null, identity: r.identity });
		result.rows.push(...r.rows);
		result.loads.push(...r.loads);
		result.errors.push(...r.errors.map((e) => `${side}#${rep}: ${e}`));
		await checkMedia(side, url, `after rep ${rep}`);
		console.log(`rep ${rep} ${side}: ${r.rows.map((x) => `${x.state} r=${x.renderer} pss=${x.renderer_pss_mb}`).join(' | ')}${r.errors.length ? ' ERR ' + r.errors[0].slice(0, 120) : ''}`);
		fs.writeFileSync(path.join(opts.out, 'ab.json'), JSON.stringify(result, null, 1));
	}
}
const summary = summarizeStates(result.rows, opts.states, opts.reps, { engine: Boolean(opts.aPid || opts.bPid) });
const perRep = Math.min(4, opts.ids.length);
const loadKeys = new Set(['wall_ms']);
for (const l of result.loads) for (const [k, v] of Object.entries(l.stages ?? {})) if (typeof v === 'number') loadKeys.add(k);
summary['load-4'] = Object.fromEntries([...loadKeys].map((k) => [k, compareLoads(result.loads, k, perRep, opts.reps)]));
// metrics that lost a rep on either side; ab.py makes such a run UNKNOWN
result.incomplete = Object.entries(summary).flatMap(([state, ms]) => Object.entries(ms).filter(([, c]) => c.incomplete).map(([k]) => `${state}.${k}`));
result.summary = summary;
result.finished_at = new Date().toISOString();
// ab.py takes the stamp only from a final ab.json that verified it (ab.py instrument)
result.harness_fault = harnessMoved(startedAs, harnessStamp(require));
if (result.harness_fault) Object.assign(result, { harness: null, measurement_deps: null });
else result.harness_verified = true;
fs.writeFileSync(path.join(opts.out, 'ab.json'), JSON.stringify(result, null, 1));
if (result.errors.length) { console.error(`[WARN] ${result.errors.length} errors; first: ${result.errors[0]}`); process.exitCode = 1; }
if (result.harness_fault) { console.error(`[FAIL] ${result.harness_fault}`); process.exitCode = 3; }
for (const [state, ms] of Object.entries(summary)) {
	for (const k of ['renderer', 'chrome_total', 'engine', 'renderer_pss_mb', 'js_heap_mb', 'begin_main_frames_per_s', 'wall_ms', 'total_ms']) {
		const s = ms[k]; if (!s || s.delta === null) continue;
		console.log(`${state.padEnd(15)} ${k.padEnd(24)} A ${s.A.mean} B ${s.B.mean} delta ${s.delta}${s.incomplete ? ` (UNKNOWN: A n=${s.A?.n ?? 0}, B n=${s.B?.n ?? 0} of ${opts.reps} reps)` : s.resolved === null ? ' (UNKNOWN: fewer than 2 samples a side)' : s.resolved ? ' (resolved)' : ` (within noise ${s.noise_2se})`}`);
	}
}
