/**
 * ANIM-CLOCK-01 live probe: how smoothly does every playhead-driven element move?
 *
 * Run from apps/webui/frontend against a real engine backend:
 *
 *   node tests/live/playhead-smoothness.mjs <port> <label> [split=0|1] [backend=http://127.0.0.1:8728]
 *
 * Starts its own vite dev server on <port> (proxying /api to the backend), loads
 * two tracks into decks 1 and 2 through window.musicDjToolsPerformance, plays
 * them Beat-Synced, settles, then samples every animation frame for MEASURE_MS.
 * Each element reports the position it last rendered from through
 * `src/lib/rb/playhead-trace.ts`; the sampler reads those once per frame, after
 * the frame's rAF callbacks and their Svelte flush have run.
 *
 * Score per element (one series per element and deck):
 *   dev = |(painted_n - painted_n-1) - tempo x (frame_n - frame_n-1)|  in ms
 * reported as p50/p95/max over every consecutive frame pair, plus holds (frames
 * the element did not move while its deck played) and visible jumps per minute
 * (dev > 15 ms and > 50 ms). It is computed per ACTUAL frame interval, so it is
 * meaningful at any frame rate; fps and frame-interval percentiles are printed
 * beside it. Clock events (snaps, slews) are the `event:*` counters the clock
 * reports, differenced over the window.
 *
 * Fails (exit 1) rather than printing numbers from an unprepared page: both
 * decks must be playing, synced, and every expected element must have reported.
 */
import { chromium } from 'playwright';
import { createServer } from 'vite';
import { writeFileSync } from 'node:fs';

const [portArg, label = 'run', splitArg = '0', backend = 'http://127.0.0.1:8728'] = process.argv.slice(2);
const PORT = Number(portArg);
const SPLIT = splitArg === '1';
const MASTER = '66b83341e26c955be715f46bf6c8f9f67722fb33'; // Adieu, 126 bpm
const FOLLOWER = 'fafa16b3781d1058fbd0c301a5c9c12cd0c2c915'; // Along Came Polly, 124 bpm
const SETTLE_MS = 6000;
const MEASURE_MS = 15000;
if (!Number.isInteger(PORT)) throw new Error('usage: node tests/live/playhead-smoothness.mjs <port> <label> [split] [backend]');

const percentile = (values, p) => {
	if (values.length === 0) return null;
	const sorted = [...values].sort((a, b) => a - b);
	return sorted[Math.min(sorted.length - 1, Math.floor(p * sorted.length))];
};
const round = (value) => (value === null ? null : Math.round(value * 100) / 100);

function scoreSeries(frames, key, rate) {
	const devs = [];
	let holds = 0;
	for (let i = 1; i < frames.length; i++) {
		const a = frames[i - 1].v[key];
		const b = frames[i].v[key];
		if (a === undefined || b === undefined) continue;
		const dt = frames[i].t - frames[i - 1].t;
		const moved = b - a;
		if (moved === 0) holds++;
		devs.push(Math.abs(moved - rate * dt));
	}
	const minutes = (frames[frames.length - 1].t - frames[0].t) / 60000;
	return {
		pairs: devs.length,
		p50_ms: round(percentile(devs, 0.5)),
		p95_ms: round(percentile(devs, 0.95)),
		max_ms: round(devs.length === 0 ? null : Math.max(...devs)),
		holds,
		jumps_over_15ms_per_min: round(devs.filter((d) => d > 15).length / minutes),
		jumps_over_50ms_per_min: round(devs.filter((d) => d > 50).length / minutes)
	};
}

const server = await createServer({
	server: { port: PORT, strictPort: true, host: '127.0.0.1', proxy: { '/api': { target: backend, changeOrigin: true, ws: true } } },
	logLevel: 'error'
});
await server.listen();
const browser = await chromium.launch({ args: ['--autoplay-policy=no-user-gesture-required'] });
const out = { label, split: SPLIT, port: PORT };
let failed = false;
try {
	const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
	const errors = [];
	page.on('pageerror', (e) => errors.push(String(e).slice(0, 300)));
	await page.addInitScript(() => {
		globalThis.__mdtPlayheadTrace = {};
	});
	await page.goto(`http://127.0.0.1:${PORT}/performance`, { waitUntil: 'domcontentloaded', timeout: 180_000 });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, { timeout: 180_000 });
	const ipc = (cmd) => page.evaluate((c) => window.musicDjToolsPerformance.dispatch(c), cmd);
	const waitLoaded = (d, id) =>
		page.waitForFunction(
			([d, id]) => {
				const s = window.musicDjToolsPerformance.query().decks[d];
				return s.stable_id === id && !s.transport_pending && (s.duration_ms ?? 0) > 0;
			},
			[d, id],
			{ timeout: 120_000 }
		);
	if (SPLIT) {
		const ui = await page.evaluate(() => window.musicDjToolsPerformance.query().ui);
		await ipc({ type: 'set_skin', ui_skin: ui.ui_skin, wave_palette: ui.wave_palette, wave_split_master: 'on' });
	}
	await ipc({ type: 'load', deck: 1, stable_id: MASTER });
	await waitLoaded(1, MASTER);
	await ipc({ type: 'load', deck: 2, stable_id: FOLLOWER });
	await waitLoaded(2, FOLLOWER);
	await ipc({ type: 'play', deck: 1, playing: true });
	await ipc({ type: 'master', deck: 1 });
	await ipc({ type: 'beat_sync', deck: 2, enabled: true });
	await ipc({ type: 'play', deck: 2, playing: true });
	await page.waitForTimeout(SETTLE_MS);
	const deckState = (d) =>
		page.evaluate((d) => {
			const s = window.musicDjToolsPerformance.query().decks[d];
			return { playing: s.playing, pos: Math.round(s.position_ms), pitch: s.pitch, sync: s.beat_sync_enabled ?? null };
		}, d);
	out.before = { 1: await deckState(1), 2: await deckState(2) };
	const frames = await page.evaluate(
		(ms) =>
			new Promise((resolve) => {
				const trace = globalThis.__mdtPlayheadTrace;
				const sampled = [];
				const channel = new MessageChannel();
				const pending = [];
				channel.port1.onmessage = () => sampled.push({ t: pending.shift(), v: { ...trace } });
				const t0 = performance.now();
				const tick = (t) => {
					pending.push(t);
					channel.port2.postMessage(0); // runs after this frame's rAF callbacks and their flush
					if (t - t0 < ms) requestAnimationFrame(tick);
					else setTimeout(() => resolve(sampled), 100);
				};
				requestAnimationFrame(tick);
			}),
		MEASURE_MS
	);
	out.after = { 1: await deckState(1), 2: await deckState(2) };
	const intervals = frames.slice(1).map((f, i) => f.t - frames[i].t);
	const seconds = (frames[frames.length - 1].t - frames[0].t) / 1000;
	out.fps = {
		fps: round(frames.length / seconds),
		frames: frames.length,
		interval_p50_ms: round(percentile(intervals, 0.5)),
		interval_p95_ms: round(percentile(intervals, 0.95)),
		interval_max_ms: round(Math.max(...intervals))
	};
	const keys = [...new Set(frames.flatMap((f) => Object.keys(f.v)))].sort();
	const expected = ['wave:1', 'wave:2', 'jog:1', 'jog:2', 'overview:1', 'overview:2', ...(SPLIT ? ['wave-partner:1'] : [])];
	const missing = expected.filter((k) => !keys.includes(k));
	out.elements = {};
	for (const key of keys.filter((k) => !k.startsWith('event:') && ['1', '2'].includes(k.split(':')[1]))) {
		const deck = Number(key.split(':')[1]);
		const rate = (out.before[deck].pitch + out.after[deck].pitch) / 2;
		out.elements[key] = scoreSeries(frames, key, rate);
	}
	const first = frames[0].v;
	const last = frames[frames.length - 1].v;
	out.events = Object.fromEntries(keys.filter((k) => k.startsWith('event:')).map((k) => [k, (last[k] ?? 0) - (first[k] ?? 0)]));
	out.events_per_min = Object.fromEntries(Object.entries(out.events).map(([k, n]) => [k, round(n / (seconds / 60))]));
	out.page_errors = errors.slice(0, 5);
	const unprepared = [];
	for (const d of [1, 2]) {
		if (!out.before[d].playing || !out.after[d].playing) unprepared.push(`deck ${d} not playing`);
	}
	if (out.before[2].sync !== true) unprepared.push('deck 2 not beat-synced');
	if (missing.length > 0) unprepared.push(`elements never reported: ${missing.join(', ')}`);
	if (unprepared.length > 0) {
		out.error = `unprepared page: ${unprepared.join('; ')}`;
		failed = true;
	}
	await page.screenshot({ path: `/tmp/playhead-smoothness-${label}.png` });
	await ipc({ type: 'play', deck: 1, playing: false });
	await ipc({ type: 'play', deck: 2, playing: false });
} catch (e) {
	out.error = String(e).slice(0, 500);
	failed = true;
} finally {
	await browser.close();
	await server.close();
}
writeFileSync(`/tmp/playhead-smoothness-${label}.json`, JSON.stringify(out, null, 1));
console.log(JSON.stringify(out));
process.exit(failed ? 1 : 0);
