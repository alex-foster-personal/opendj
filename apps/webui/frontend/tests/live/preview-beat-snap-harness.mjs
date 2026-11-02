/**
 * PVSNAP spike: does a snapped preview click SOUND in phase with the master,
 * on a real audio clock, under load? Runs in headless Chromium in seconds,
 * with none of the app around it.
 *
 * What it builds, in one page:
 *   - a "master" click track (accented downbeats) playing from a mid-track
 *     position, and a "preview" click track at a different tempo;
 *   - the same planner the app uses (`preview-beat-snap.ts`, bundled by
 *     esbuild exactly as `stem-decode-workers.mjs` bundles its module);
 *   - a recorder AudioWorklet that taps master and preview onto separate
 *     channels, so onsets can be paired sample-accurately.
 * Then it fires N "clicks" at random points of the preview track, each one
 * planned and started the way the app would, and measures every preview
 * onset against the nearest master onset.
 *
 * Load knobs (simulate the full app without the full app):
 *   --jank-ms N --jank-every-ms M  busy-loop the main thread N ms every M ms
 *   --decode-delay-ms N            await N ms between click and plan (a cold
 *                                  fetch + decode; exercises catch-up)
 *   --audio-load-us N              an AudioWorklet that burns N microseconds
 *                                  per render quantum, standing in for four
 *                                  deck stretch worklets on the audio thread
 *   --cpu-workers N                N Web Workers spinning, stealing cores
 *   --master buffer|stretch        the master voice: a plain buffer source, or
 *                                  the deck's real Signalsmith stretch worklet
 *                                  (does the deck's audible output sit where
 *                                  its control clock says?)
 *   --preview buffer|stretch       the preview voice: playbackRate (pitch moves
 *                                  with tempo) or Signalsmith (MT, key held)
 *   --master-rate R                the master's tempo ratio (a pitched deck)
 *   --lead-ms N                    click-to-start lead (default: the product's)
 *   --clicks N --mode bar|beat --master-bpm X --preview-bpm Y --seed S
 *   --naive                        the pre-snap behavior (start at the click,
 *                                  tempo only): the control that must fail
 *
 * Output: one JSON line and a table: per-click click-to-sound latency, phase
 * error p50 / p95 / max in ms, and the share of preview onsets whose accent
 * (downbeat or not) matches the master onset they sit on.
 *
 * Exit codes, because a run that measured nothing must not read as a pass:
 *   0  measured, and within budget (p95 <= --budget-ms, default 5 ms, about
 *      where two hits start to read as a flam; accents 100% in bar mode)
 *   1  measured, and OUT of budget
 *   3  UNAVAILABLE: Chromium did not launch, or no onsets were paired
 *
 * Run (from apps/webui/frontend):
 *   node tests/live/preview-beat-snap-harness.mjs
 *   node tests/live/preview-beat-snap-harness.mjs --decode-delay-ms 700 --jank-ms 40 --jank-every-ms 100 --audio-load-us 1500
 *   node tests/live/preview-beat-snap-harness.mjs --naive   # must exit 1
 *   PVSNAP_CHROMIUM=/path/to/chrome ...   # when the pinned browser is absent
 */

import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';

import { chromium } from '@playwright/test';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.resolve(HERE, '../..');

const { values: a } = parseArgs({
	options: {
		clicks: { type: 'string', default: '10' },
		mode: { type: 'string', default: 'bar' },
		'master-bpm': { type: 'string', default: '128' },
		'preview-bpm': { type: 'string', default: '124' },
		'jank-ms': { type: 'string', default: '0' },
		'jank-every-ms': { type: 'string', default: '100' },
		'decode-delay-ms': { type: 'string', default: '0' },
		'audio-load-us': { type: 'string', default: '0' },
		'cpu-workers': { type: 'string', default: '0' },
		'budget-ms': { type: 'string', default: '5' },
		seed: { type: 'string', default: '7' },
		master: { type: 'string', default: 'buffer' },
		preview: { type: 'string', default: 'buffer' },
		'master-rate': { type: 'string', default: '1' },
		'lead-ms': { type: 'string' },
		naive: { type: 'boolean', default: false }
	}
});
const cfg = {
	clicks: Number(a.clicks),
	mode: a.mode,
	masterBpm: Number(a['master-bpm']),
	previewBpm: Number(a['preview-bpm']),
	jankMs: Number(a['jank-ms']),
	jankEveryMs: Number(a['jank-every-ms']),
	decodeDelayMs: Number(a['decode-delay-ms']),
	audioLoadUs: Number(a['audio-load-us']),
	cpuWorkers: Number(a['cpu-workers']),
	seed: Number(a.seed),
	naive: a.naive,
	master: a.master,
	preview: a.preview,
	masterRate: Number(a['master-rate']),
	leadMs: a['lead-ms'] === undefined ? null : Number(a['lead-ms'])
};
const budgetMs = Number(a['budget-ms']);

async function bundle() {
	const out = path.join(tmpdir(), 'pvsnap-harness-bundle.mjs');
	await new Promise((resolve, reject) => {
		const proc = spawn(
			path.join(FRONTEND, 'node_modules/.bin/esbuild'),
			[
				path.join(HERE, 'preview-beat-snap-harness-entry.ts'),
				'--bundle',
				'--format=iife',
				'--global-name=PVSNAP',
				'--target=chrome120',
				`--outfile=${out}`,
				'--log-level=warning'
			],
			{ cwd: FRONTEND, stdio: ['ignore', 'inherit', 'inherit'] }
		);
		proc.on('exit', (code) => (code === 0 ? resolve() : reject(new Error(`esbuild ${code}`))));
	});
	return readFile(out, 'utf8');
}

/** Everything below runs IN the page. */
async function inPage(cfg) {
	const { planPreviewSnap, PREVIEW_SNAP_LEAD_SEC, buildStretchSchedule, STRETCH_NODE_OPTIONS } = globalThis.PVSNAP;
	const lead = cfg.leadMs === null ? PREVIEW_SNAP_LEAD_SEC : cfg.leadMs / 1000;
	let seed = cfg.seed;
	const rand = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);

	const ctx = new AudioContext({ latencyHint: 'interactive' });
	await ctx.resume();
	const sr = ctx.sampleRate;

	function grid(count, bpm, offset) {
		const beat = 60 / bpm;
		return Array.from({ length: count }, (_, i) => ({ n: (i % 4) + 1, bpm, t: offset + i * beat }));
	}
	/** A click track: a 2 ms burst on every beat, louder on the downbeat. */
	function clickBuffer(beats, seconds) {
		const buf = ctx.createBuffer(1, Math.ceil(seconds * sr), sr);
		const d = buf.getChannelData(0);
		const w = Math.round(0.002 * sr);
		for (const b of beats) {
			const s = Math.round(b.t * sr);
			const amp = b.n === 1 ? 0.9 : 0.4;
			for (let k = 0; k < w && s + k < d.length; k += 1) d[s + k] = amp;
		}
		return buf;
	}

	const masterGrid = grid(700, cfg.masterBpm, 0.137);
	const previewGrid = grid(600, cfg.previewBpm, 0.291);
	const masterBuf = clickBuffer(masterGrid, masterGrid.at(-1).t + 1);
	const previewBuf = clickBuffer(previewGrid, previewGrid.at(-1).t + 1);

	const RECORDER = `
		class Rec extends AudioWorkletProcessor {
			constructor(o) { super(); this.cap = o.processorOptions.cap; this.l = new Float32Array(this.cap); this.r = new Float32Array(this.cap); this.n = 0; this.first = -1;
				this.port.onmessage = () => this.port.postMessage({ first: this.first, n: this.n, l: this.l.slice(0, this.n), r: this.r.slice(0, this.n) }); }
			process(inputs) { const i = inputs[0]; if (this.first < 0) this.first = currentFrame;
				const len = (i[0] || []).length || 128; if (this.n + len > this.cap) return true;
				if (i[0]) this.l.set(i[0], this.n); if (i[1]) this.r.set(i[1], this.n); this.n += len; return true; } }
		registerProcessor('rec', Rec);
		class Burn extends AudioWorkletProcessor {
			constructor(o) { super(); this.us = o.processorOptions.us; }
			process() { const end = Date.now() + this.us / 1000; let x = 0; while (Date.now() < end) x += Math.random(); return true; } }
		registerProcessor('burn', Burn);`;
	await ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([RECORDER], { type: 'text/javascript' })));

	const seconds = cfg.clicks * 2.6 + 3;
	const rec = new AudioWorkletNode(ctx, 'rec', {
		numberOfInputs: 1,
		numberOfOutputs: 1,
		channelCount: 2,
		channelCountMode: 'explicit',
		processorOptions: { cap: Math.ceil(seconds * sr) + sr }
	});
	const merger = ctx.createChannelMerger(2);
	merger.connect(rec);
	const mute = ctx.createGain();
	mute.gain.value = 0;
	rec.connect(mute).connect(ctx.destination);
	if (cfg.audioLoadUs > 0) {
		const burn = new AudioWorkletNode(ctx, 'burn', { processorOptions: { us: cfg.audioLoadUs } });
		burn.connect(mute);
	}
	const workers = [];
	for (let w = 0; w < cfg.cpuWorkers; w += 1) {
		workers.push(new Worker(URL.createObjectURL(new Blob(['for(;;){Math.random()}'], { type: 'text/javascript' }))));
	}
	let jankTimer = null;
	if (cfg.jankMs > 0) {
		jankTimer = setInterval(() => {
			const end = performance.now() + cfg.jankMs;
			while (performance.now() < end) Math.random();
		}, cfg.jankEveryMs);
	}

	// The master: mid-track, as if it had been playing a while.
	const masterStartCtx = ctx.currentTime + 0.2;
	const masterStartCtxRef = { value: masterStartCtx };
	const masterStartPos = masterGrid[160].t + 0.123;
	let stretchFactory = null;
	const stretchLatency = [];
	async function stretchNode(buf) {
		stretchFactory ??= (await import('/SignalsmithStretch.mjs')).default;
		const node = await stretchFactory(ctx, STRETCH_NODE_OPTIONS);
		const ch = buf.getChannelData(0);
		await node.addBuffers([ch, ch]);
		stretchLatency.push(await node.latency());
		return node;
	}
	if (cfg.master === 'stretch') {
		const node = await stretchNode(masterBuf);
		node.connect(merger, 0, 0);
		const startAt = ctx.currentTime + 0.4;
		node.schedule(buildStretchSchedule(startAt, { active: true, input: masterStartPos, rate: cfg.masterRate }));
		masterStartCtxRef.value = startAt;
	} else {
		const master = ctx.createBufferSource();
		master.buffer = masterBuf;
		master.playbackRate.value = cfg.masterRate;
		master.connect(merger, 0, 0);
		master.start(masterStartCtx, masterStartPos);
	}
	const masterPosAt = (t) => masterStartPos + (t - masterStartCtxRef.value) * cfg.masterRate;
	const previewStretch = cfg.preview === 'stretch' ? await stretchNode(previewBuf) : null;
	if (previewStretch !== null) previewStretch.connect(merger, 0, 1);

	const clicks = [];
	let current = null;
	await new Promise((r) => setTimeout(r, 600));
	for (let c = 0; c < cfg.clicks; c += 1) {
		const clickCtx = ctx.currentTime;
		const clickSec = 20 + rand() * (previewGrid.at(-40).t - 20);
		if (cfg.decodeDelayMs > 0) await new Promise((r) => setTimeout(r, cfg.decodeDelayMs));
		// From here to start() is ONE synchronous task, as in the app: nothing
		// can run between the plan and the schedule.
		const now = ctx.currentTime;
		const startAt = now + lead;
		let plan;
		if (cfg.naive) {
			plan = { ok: true, startAtContextSec: startAt, offsetSec: clickSec, rate: (cfg.masterBpm * cfg.masterRate) / cfg.previewBpm };
		} else {
			plan = planPreviewSnap({
				masterGrid,
				previewGrid,
				masterPositionAtStartSec: masterPosAt(startAt),
				masterTempoRatio: cfg.masterRate,
				clickSec,
				nowContextSec: now,
				startAtContextSec: startAt,
				pitchRangePct: 16,
				mode: cfg.mode
			});
		}
		if (!plan.ok) {
			clicks.push({ clickCtx, refused: plan.reason });
			await new Promise((r) => setTimeout(r, 2000));
			continue;
		}
		const lateBySec = ctx.currentTime - plan.startAtContextSec; // > 0: the schedule was already past
		if (previewStretch !== null) {
			previewStretch.schedule(
				buildStretchSchedule(plan.startAtContextSec, { active: true, input: plan.offsetSec, rate: plan.rate })
			);
		} else {
			const src = ctx.createBufferSource();
			src.buffer = previewBuf;
			src.playbackRate.value = plan.rate;
			src.connect(merger, 0, 1);
			src.start(plan.startAtContextSec, plan.offsetSec);
			if (current !== null) current.stop(plan.startAtContextSec);
			current = src;
		}
		clicks.push({ clickCtx, startAt: plan.startAtContextSec, lateBySec });
		await new Promise((r) => setTimeout(r, 1900 + rand() * 600));
	}
	if (jankTimer !== null) clearInterval(jankTimer);
	for (const w of workers) w.terminate();
	await new Promise((r) => setTimeout(r, 300));
	const data = await new Promise((resolve) => {
		rec.port.onmessage = (e) => resolve(e.data);
		rec.port.postMessage('dump');
	});
	await ctx.close();
	return {
		sr,
		first: data.first,
		l: Array.from(data.l),
		r: Array.from(data.r),
		clicks,
		baseLatency: ctx.baseLatency,
		stretchLatency
	};
}

/**
 * Onsets: first sample over the threshold after 20 ms of quiet, with the
 * burst's peak. An accent (downbeat) is a peak well above the channel's
 * median, read RELATIVE to the channel because a stretched voice smears a
 * 2 ms burst and lowers every peak by an amount the source never set.
 */
function onsets(x, sr, first) {
	const out = [];
	const quiet = Math.round(0.02 * sr);
	let lastLoud = -Infinity;
	for (let i = 0; i < x.length; i += 1) {
		const v = Math.abs(x[i]);
		if (v > 0.1) {
			if (i - lastLoud > quiet) {
				let peak = 0;
				for (let k = i; k < Math.min(x.length, i + Math.round(0.01 * sr)); k += 1) peak = Math.max(peak, Math.abs(x[k]));
				out.push({ t: (first + i) / sr, peak });
			}
			lastLoud = i;
		}
	}
	const peaks = out.map((o) => o.peak).sort((p, q) => p - q);
	const median = peaks[Math.floor(peaks.length / 2)] ?? 0;
	for (const o of out) o.accent = o.peak > median * 1.4;
	return out;
}

function pct(sorted, p) {
	if (sorted.length === 0) return NaN;
	return sorted[Math.min(sorted.length - 1, Math.floor(p * sorted.length))];
}

async function main() {
	const code = await bundle();
	let browser;
	try {
		browser = await chromium.launch({
			args: ['--autoplay-policy=no-user-gesture-required'],
			// A box whose Chromium does not match the pinned Playwright build
			// (a cloud container, a CI image) names its own binary here.
			executablePath: process.env.PVSNAP_CHROMIUM || undefined
		});
	} catch (e) {
		console.log(`UNAVAILABLE: chromium did not launch (${e.message})`);
		process.exit(3);
	}
	// AudioWorklet needs a secure context, which about:blank is not and
	// http://127.0.0.1 is, so the page is served rather than set.
	const stretchModule = await readFile(path.join(FRONTEND, 'node_modules/signalsmith-stretch/SignalsmithStretch.mjs'));
	const server = createServer((req, res) => {
		if (req.url === '/SignalsmithStretch.mjs') {
			res.writeHead(200, { 'content-type': 'text/javascript' });
			res.end(stretchModule);
			return;
		}
		res.writeHead(200, { 'content-type': 'text/html' });
		res.end(`<!doctype html><meta charset="utf-8"><title>pvsnap</title><script>${code}</script>`);
	});
	await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
	const page = await browser.newPage();
	await page.goto(`http://127.0.0.1:${server.address().port}/`);
	const res = await page.evaluate(inPage, cfg);
	await browser.close();
	server.close();

	const m = onsets(res.l, res.sr, res.first);
	const p = onsets(res.r, res.sr, res.first);
	const errors = [];
	const signed = [];
	let accentPairs = 0;
	let accentMatch = 0;
	const perClick = [];
	const started = res.clicks.filter((c) => c.startAt !== undefined);
	for (let i = 0; i < started.length; i += 1) {
		const c = started[i];
		const end = started[i + 1]?.startAt ?? Infinity;
		const mine = p.filter((o) => o.t >= c.startAt - 0.001 && o.t < end - 0.001);
		const errs = [];
		for (const o of mine) {
			let best = null;
			for (const mo of m) if (best === null || Math.abs(mo.t - o.t) < Math.abs(best.t - o.t)) best = mo;
			if (best === null) continue;
			const e = (o.t - best.t) * 1000;
			errs.push(e);
			signed.push(e);
			errors.push(Math.abs(e));
			accentPairs += 1;
			if (best.accent === o.accent) accentMatch += 1;
		}
		const firstOnset = mine[0]?.t;
		perClick.push({
			click: i + 1,
			click_to_start_ms: Math.round((c.startAt - c.clickCtx) * 1000),
			late_schedule_ms: Math.round(c.lateBySec * 1000 * 10) / 10,
			first_onset_after_start_ms: firstOnset === undefined ? null : Math.round((firstOnset - c.startAt) * 1000),
			onsets: errs.length,
			worst_ms: errs.length ? Math.round(Math.max(...errs.map(Math.abs)) * 100) / 100 : null
		});
	}
	const refused = res.clicks.filter((c) => c.refused !== undefined).map((c) => c.refused);
	errors.sort((x, y) => x - y);
	const summary = {
		config: cfg,
		sample_rate: res.sr,
		stretch_latency_ms: res.stretchLatency.map((x) => Math.round(x * 10000) / 10),
		master_onsets: m.length,
		paired_onsets: errors.length,
		refused,
		phase_error_ms: { p50: pct(errors, 0.5), p95: pct(errors, 0.95), max: errors.at(-1) ?? NaN },
		/** Positive: the preview sounds AFTER the master. */
		mean_signed_ms: signed.length ? signed.reduce((x, y) => x + y, 0) / signed.length : NaN,
		accent_match_pct: accentPairs ? Math.round((accentMatch / accentPairs) * 1000) / 10 : NaN
	};
	console.table(perClick);
	console.log(JSON.stringify(summary));
	if (errors.length === 0) {
		console.log('UNAVAILABLE: no preview onsets were paired, so nothing was measured');
		process.exit(3);
	}
	const accentOk = cfg.mode !== 'bar' || summary.accent_match_pct === 100;
	const within = summary.phase_error_ms.p95 <= budgetMs && accentOk;
	console.log(
		within
			? `PASS: p95 ${summary.phase_error_ms.p95.toFixed(3)} ms <= ${budgetMs} ms, accents ${summary.accent_match_pct}%`
			: `FAIL: p95 ${summary.phase_error_ms.p95.toFixed(3)} ms (budget ${budgetMs}), accents ${summary.accent_match_pct}%`
	);
	process.exit(within ? 0 : 1);
}

await main();
