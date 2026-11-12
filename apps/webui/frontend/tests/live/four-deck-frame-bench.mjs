/**
 * PERF-CAPTURE-10504 live bench: rAF frame budget with four decks playing.
 *
 * Run from apps/webui/frontend against a real engine backend:
 *
 *   node tests/live/four-deck-frame-bench.mjs <port> [backend=http://127.0.0.1:8728] [manifest=tests/e2e/fixtures/performance-playwright-data/fixture-manifest.json]
 *   pnpm test:live:four-deck-frame <port> [backend] [manifest]       (the same)
 *   xvfb-run -a pnpm test:live:four-deck-frame <port> [backend]      (CI host form)
 *
 * Backend on the deckload fixture data dir (same as playwright.performance.config.ts):
 *   uv run --no-sync python -m apps.webui.frontend.tests.e2e.support.deckload_fixture --data-dir <dir> --manifest <dir>/fixture-manifest.json
 *   uv run --no-sync python -m apps.engine_core serve --data-dir <dir> --host 127.0.0.1 --port <p>
 *
 * Starts its own vite dev server on <port> (proxying /api), opens headless
 * Chromium, measures the idle refresh interval, loads the manifest's first two
 * tracks onto decks 1-4, plays all four, settles, then samples rAF timestamp
 * deltas and waveRowFrame callback wall time for MEASURE_MS. Prints ONE JSON
 * line on stdout. On any failure (including fewer than four decks playing) it
 * prints the named error to stderr, no JSON, and exits 1.
 */
import { chromium } from 'playwright';
import { createServer } from 'vite';
import { readFileSync } from 'node:fs';
import { assertFourDecksPlaying, summarizeFrameBench, BENCH } from './frame-bench-stats.mjs';

const [
	portArg,
	backend = 'http://127.0.0.1:8728',
	manifestPath = 'tests/e2e/fixtures/performance-playwright-data/fixture-manifest.json'
] = process.argv.slice(2);
const PORT = Number(portArg);
const IDLE_MS = 2000;
const SETTLE_MS = 3000;
const MEASURE_MS = 10000;
const DECKS = [1, 2, 3, 4];
if (!Number.isInteger(PORT))
	throw new Error('usage: node tests/live/four-deck-frame-bench.mjs <port> [backend] [manifest]');

function readTrackIds(path) {
	let manifest;
	try {
		manifest = JSON.parse(readFileSync(path, 'utf8'));
	} catch (e) {
		throw new Error(`${BENCH}: cannot read manifest ${path}: ${e.message}`);
	}
	const tracks = manifest.tracks ?? [];
	if (tracks.length < 2)
		throw new Error(`${BENCH}: manifest ${path} has ${tracks.length} tracks, need 2`);
	return [tracks[0].stable_id, tracks[1].stable_id];
}

const sampleRafDeltas = (page, ms) =>
	page.evaluate(
		(ms) =>
			new Promise((resolve) => {
				const bench = window.__mdtFrameBench;
				bench.waveRowMs = 0;
				bench.recording = true;
				const deltas = [];
				let t0 = null;
				let last = null;
				const tick = (t) => {
					if (t0 === null) t0 = t;
					else deltas.push(t - last);
					last = t;
					if (t - t0 < ms) requestAnimationFrame(tick);
					else {
						bench.recording = false;
						resolve({ deltas, waveRowMs: bench.waveRowMs, sampleMs: last - t0 });
					}
				};
				requestAnimationFrame(tick);
			}),
		ms
	);

const [trackA, trackB] = readTrackIds(manifestPath);
const server = await createServer({
	server: {
		port: PORT,
		strictPort: true,
		host: '127.0.0.1',
		proxy: { '/api': { target: backend, changeOrigin: true, ws: true } }
	},
	logLevel: 'error'
});
await server.listen();
const browser = await chromium.launch({
	headless: true,
	args: ['--autoplay-policy=no-user-gesture-required']
});
let failed = false;
try {
	const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
	page.on('pageerror', (e) => process.stderr.write(`pageerror: ${String(e).slice(0, 300)}\n`));
	await page.addInitScript(() => {
		const bench = { waveRowMs: 0, recording: false };
		window.__mdtFrameBench = bench;
		const original = window.requestAnimationFrame.bind(window);
		window.requestAnimationFrame = (callback) =>
			original((timestamp) => {
				if (!bench.recording || callback.name !== 'waveRowFrame') return callback(timestamp);
				const start = performance.now();
				try {
					return callback(timestamp);
				} finally {
					bench.waveRowMs += performance.now() - start;
				}
			});
	});
	await page.goto(`http://127.0.0.1:${PORT}/performance`, {
		waitUntil: 'domcontentloaded',
		timeout: 180_000
	});
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
		timeout: 180_000
	});
	const idle = await sampleRafDeltas(page, IDLE_MS);
	const ipc = (cmd) => page.evaluate((c) => window.musicDjToolsPerformance.dispatch(c), cmd);
	const waitLoaded = (d, id) =>
		page.waitForFunction(
			([d, id]) => {
				const s = window.musicDjToolsPerformance.query().decks[d];
				return s?.stable_id === id && !s.transport_pending && (s.duration_ms ?? 0) > 0;
			},
			[d, id],
			{ timeout: 120_000 }
		);
	for (const deck of DECKS) {
		const id = deck % 2 === 1 ? trackA : trackB;
		await ipc({ type: 'load', deck, stable_id: id });
		// A load failure is reported by the named deck-count guard below, not as a raw timeout.
		await waitLoaded(deck, id).catch((err) =>
			console.error(`four-deck-frame-bench: deck ${deck} did not load: ${err.message}`)
		);
	}
	for (const deck of DECKS) await ipc({ type: 'play', deck, playing: true });
	await page.waitForTimeout(SETTLE_MS);
	const decks = () => page.evaluate(() => window.musicDjToolsPerformance.query().decks);
	assertFourDecksPlaying(await decks());
	const sample = await sampleRafDeltas(page, MEASURE_MS);
	assertFourDecksPlaying(await decks());
	const row = summarizeFrameBench({ ...sample, idleDeltas: idle.deltas });
	for (const deck of DECKS) await ipc({ type: 'play', deck, playing: false });
	console.log(JSON.stringify(row));
} catch (e) {
	process.stderr.write(`${e?.message ?? e}\n`);
	failed = true;
} finally {
	await browser.close();
	await server.close();
}
process.exit(failed ? 1 : 0);
