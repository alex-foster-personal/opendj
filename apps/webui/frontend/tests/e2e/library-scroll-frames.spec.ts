// requirement: PERF-CAPTURE-04
/**
 * Library scroll frame bench (issue #10230, epic #9377). Opens All Tracks on the
 * 1k fixture, samples rAF deltas and long tasks while `page.mouse.wheel` scrolls
 * the track list (120 px every 16 ms, 5 s down then 5 s up), counts non-passive
 * wheel listeners on window and document via CDP, and fails when a metric
 * exceeds its cap in `tests/fixtures/library-scroll-frames.json`.
 *
 * Run::
 *
 *     pnpm exec playwright test --config tests/e2e/playwright.playlist-switch-latency.config.ts library-scroll-frames.spec.ts
 *
 * Writes `test-results/library-scroll-frames.json`: interval_p95_ms,
 * interval_max_ms, long_tasks_over_50ms, rows_mounted, blocking_wheel_listeners,
 * or `status: 'refused'` with the reason.
 */
import { expect, test, type Page } from '@playwright/test';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { PLAYLIST_SWITCH_BENCH_ORIGIN } from './playwright.playlist-switch-latency.config';
import {
	checkCaps,
	refusalReason,
	summarizeFrameIntervals,
	LONG_FRAME_MS
} from './support/scroll-frame-stats.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const COMMITTED_FIXTURE_PATH = join(
	FRONTEND_ROOT,
	'tests',
	'fixtures',
	'library-scroll-frames.json'
);
const DEFAULT_OUT_PATH = join(FRONTEND_ROOT, 'test-results', 'library-scroll-frames.json');
const OUT_PATH = resolve(process.env.LIBRARY_SCROLL_FRAMES_OUT ?? DEFAULT_OUT_PATH);

const WHEEL_STEP_PX = 120;
const WHEEL_INTERVAL_MS = 16;
const PHASE_MS = 5_000;

const CAPS: Record<string, number> = JSON.parse(readFileSync(COMMITTED_FIXTURE_PATH, 'utf8')).caps;

interface ScrollBenchState {
	deltas: number[];
	longTasks: number[];
	stop: boolean;
	longtaskSupported: boolean;
}

interface BenchWindow extends Window {
	__scrollBench?: ScrollBenchState;
	__mdtPerfLog?: () => unknown;
}

interface ListenerSite {
	target: string;
	type: string;
	scriptId: string;
	lineNumber: number;
	columnNumber: number;
}

async function bootAllTracks(page: Page): Promise<void> {
	await page.addInitScript(() => localStorage.removeItem('odj.brand-launch.v1'));
	await page.goto('/performance', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
		timeout: 60_000
	});
	await page.waitForFunction(
		() => typeof (window as BenchWindow).__mdtPerfLog === 'function',
		undefined,
		{ timeout: 60_000 }
	);
	await expect(page.getByTestId('playlist-all-tracks')).toBeVisible({ timeout: 60_000 });
	await page.getByTestId('playlist-all-tracks').click();
	await expect(page.getByTestId('track-row').first()).toBeVisible({ timeout: 30_000 });
	await page.waitForFunction(() => {
		const row = document.querySelector<HTMLElement>('[data-testid="track-row"]');
		return row !== null && (row.innerText?.trim().length ?? 0) > 0;
	});
	await page.evaluate(
		() =>
			new Promise<void>((done) => {
				requestAnimationFrame(() => requestAnimationFrame(() => done()));
			})
	);
}

async function readBlockingWheelListeners(page: Page): Promise<ListenerSite[]> {
	const cdp = await page.context().newCDPSession(page);
	const sites: ListenerSite[] = [];
	for (const target of ['window', 'document']) {
		const { result } = await cdp.send('Runtime.evaluate', { expression: target });
		if (result.objectId === undefined) throw new Error(`CDP gave no objectId for ${target}`);
		const { listeners } = await cdp.send('DOMDebugger.getEventListeners', {
			objectId: result.objectId
		});
		for (const l of listeners) {
			if ((l.type === 'wheel' || l.type === 'mousewheel') && l.passive === false) {
				sites.push({
					target,
					type: l.type,
					scriptId: l.scriptId,
					lineNumber: l.lineNumber,
					columnNumber: l.columnNumber
				});
			}
		}
		await cdp.send('Runtime.releaseObject', { objectId: result.objectId });
	}
	await cdp.detach();
	return sites;
}

async function readScrollTop(page: Page): Promise<number> {
	return await page.evaluate(() => {
		const wrap = document.querySelector<HTMLElement>('.table-wrap');
		if (wrap === null) throw new Error('no .table-wrap scroll container');
		return wrap.scrollTop;
	});
}

/**
 * Moves the mouse to the centre of the part of `.table-wrap` that is actually on
 * screen. In /performance at 1280x800 the list panel clips the wrap to a thin strip,
 * so the wrap's own centre (or a row's) can sit over another element or off-viewport.
 */
async function hoverVisibleScrollContainer(page: Page): Promise<void> {
	const point = await page.evaluate(() => {
		const wrap = document.querySelector<HTMLElement>('.table-wrap');
		if (wrap === null) throw new Error('no .table-wrap scroll container');
		let { left, top, right, bottom } = wrap.getBoundingClientRect();
		for (let el = wrap.parentElement; el !== null; el = el.parentElement) {
			if (getComputedStyle(el).overflowY === 'visible') continue;
			const r = el.getBoundingClientRect();
			left = Math.max(left, r.left);
			top = Math.max(top, r.top);
			right = Math.min(right, r.right);
			bottom = Math.min(bottom, r.bottom);
		}
		right = Math.min(right, window.innerWidth);
		bottom = Math.min(bottom, window.innerHeight);
		if (right <= left || bottom <= top) throw new Error('.table-wrap has no visible area');
		const x = (left + right) / 2;
		const y = (top + bottom) / 2;
		const hit = document.elementFromPoint(x, y);
		if (hit === null || !wrap.contains(hit)) {
			throw new Error(`point ${x},${y} over .table-wrap hits ${hit?.tagName ?? 'nothing'}`);
		}
		return { x, y };
	});
	await page.mouse.move(point.x, point.y);
}

/** Wheels for one phase and returns the scrollTop it ends on. */
async function wheelFor(page: Page, deltaY: number): Promise<number> {
	const deadline = Date.now() + PHASE_MS;
	while (Date.now() < deadline) {
		await page.mouse.wheel(0, deltaY);
		await page.waitForTimeout(WHEEL_INTERVAL_MS);
	}
	return await readScrollTop(page);
}

function writePayload(payload: Record<string, unknown>): void {
	if (resolve(OUT_PATH) === resolve(COMMITTED_FIXTURE_PATH)) {
		throw new Error(
			`Refusing to write scroll bench results over the committed caps fixture ${COMMITTED_FIXTURE_PATH}`
		);
	}
	mkdirSync(dirname(OUT_PATH), { recursive: true });
	writeFileSync(OUT_PATH, `${JSON.stringify(payload, null, 2)}\n`);
	// eslint-disable-next-line no-console
	console.log(`[library-scroll-frames] ${JSON.stringify(payload)} -> ${OUT_PATH}`);
}

// REQ: PERF-CAPTURE-04
test('All Tracks wheel scroll frame interval p95 and blocking wheel listeners stay under caps', async ({
	page
}) => {
	test.setTimeout(180_000);
	await bootAllTracks(page);

	const listenerSites = await readBlockingWheelListeners(page);
	const blocking_wheel_listeners = listenerSites.length;
	const rows_mounted = await page.evaluate(
		() =>
			document.querySelectorAll('[data-testid="track-table"] tbody [data-testid="track-row"]')
				.length
	);

	await page.evaluate(
		() =>
			new Promise<void>((done) => {
				const state: ScrollBenchState = {
					deltas: [],
					longTasks: [],
					stop: false,
					longtaskSupported: PerformanceObserver.supportedEntryTypes.includes('longtask')
				};
				(window as BenchWindow).__scrollBench = state;
				if (state.longtaskSupported) {
					new PerformanceObserver((list) => {
						for (const entry of list.getEntries()) state.longTasks.push(entry.duration);
					}).observe({ type: 'longtask', buffered: false });
				}
				requestAnimationFrame((first) => {
					let last = first;
					const tick = (now: number): void => {
						if (state.stop) return;
						state.deltas.push(now - last);
						last = now;
						requestAnimationFrame(tick);
					};
					requestAnimationFrame(tick);
					done();
				});
			})
	);

	await hoverVisibleScrollContainer(page);
	const startTop = await readScrollTop(page);
	const maxTop = await wheelFor(page, WHEEL_STEP_PX);
	await wheelFor(page, -WHEEL_STEP_PX);
	const scrolled_px = maxTop - startTop;

	const sampled = await page.evaluate(() => {
		const state = (window as BenchWindow).__scrollBench;
		if (state === undefined) throw new Error('scroll bench sampler missing');
		state.stop = true;
		return {
			deltas: state.deltas,
			longTasks: state.longTasks,
			longtaskSupported: state.longtaskSupported
		};
	});
	if (!sampled.longtaskSupported) {
		throw new Error(
			'PerformanceObserver does not support longtask; refusing to report 0 long tasks'
		);
	}

	const frames = sampled.deltas.length;
	const reason = refusalReason({ frames, rowsMounted: rows_mounted, scrolledPx: scrolled_px });
	if (reason !== null) {
		writePayload({
			status: 'refused',
			reason,
			frames,
			rows_mounted,
			scrolled_px,
			blocking_wheel_listeners,
			blocking_wheel_listener_sites: listenerSites
		});
		throw new Error(reason);
	}

	const stats = summarizeFrameIntervals(sampled.deltas);
	const verdicts = checkCaps(
		{ interval_p95_ms: stats.interval_p95_ms, blocking_wheel_listeners },
		CAPS
	);
	writePayload({
		status: 'measured',
		...stats,
		long_tasks_over_50ms: sampled.longTasks.filter((d) => d > LONG_FRAME_MS).length,
		rows_mounted,
		blocking_wheel_listeners,
		blocking_wheel_listener_sites: listenerSites,
		scrolled_px,
		tracks: Number(process.env.PLAYLIST_SWITCH_BENCH_TRACKS ?? 1000),
		origin: PLAYLIST_SWITCH_BENCH_ORIGIN,
		wheel: { step_px: WHEEL_STEP_PX, interval_ms: WHEEL_INTERVAL_MS, phase_ms: PHASE_MS },
		caps: CAPS,
		verdicts
	});
	for (const v of verdicts) {
		// eslint-disable-next-line no-console
		console.log(`[library-scroll-frames] ${v.arithmetic}`);
	}
	for (const v of verdicts) expect(v.ok, v.arithmetic).toBe(true);
});
