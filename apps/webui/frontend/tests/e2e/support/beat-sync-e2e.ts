/**
 * Shared drivers for the Beat Sync e2e specs (round 2 adversarial hardening).
 *
 * The grid source is `/anlz` ONLY. That is the payload the deck itself loads,
 * and since #3561 the deck refuses the legacy `/beatgrid-fallback` grid for an
 * unmapped track whose own lane is `missing`. Reading the fallback here (as
 * `analyzed-track.ts` does for non-sync specs) would select tracks the deck
 * will show WITHOUT a grid, so a sync spec would fail for a reason that has
 * nothing to do with sync. In fixture mode the grid exists only when the
 * fixture was built with `--seed-own-beatgrid` (playwright.performance.config.ts
 * OWN_BEATGRID_SEEDED); without it the specs skip and say so.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import type {
	PerformanceCommand,
	PerformanceDeckSnapshot,
	PerformanceState
} from '../../../src/lib/rb/performance-ipc.svelte';
import type { DeckId } from '../../../src/lib/rb/deck-slots';

export const API_BASE = process.env.PERFORMANCE_E2E_API_BASE ?? 'http://127.0.0.1:8686';
const UI_BASE = (
	process.env.PERFORMANCE_E2E_BASE_URL ??
	process.env.PERFORMANCE_E2E_UI_BASE ??
	''
).replace(/\/$/, '');

export interface GridTrack {
	stable_id: string;
	title: string;
	bpm: number;
	beatgrid_ms: number[];
	beat_numbers: number[];
}

interface AnlzBeat {
	n: number;
	bpm: number;
	t: number;
}

/** The fixture track with this title, with its `/anlz` grid, or a skip. */
export async function ownGridTrack(request: APIRequestContext, title: string): Promise<GridTrack> {
	const listing = await request.get(`${API_BASE}/api/v1/tracks?limit=1000&available=true`);
	expect(listing.ok(), 'track listing').toBeTruthy();
	const items = (
		(await listing.json()) as {
			items: { stable_id: string; title: string | null; bpm: number | null }[];
		}
	).items;
	const row = items.find((item) => item.title === title);
	test.skip(row === undefined, `fixture track ${title} is not in this library`);
	const anlz = await request.get(
		`${API_BASE}/api/v1/tracks/${encodeURIComponent(row!.stable_id)}/anlz?points=100`
	);
	expect(anlz.ok(), `/anlz for ${title}`).toBeTruthy();
	const payload = (await anlz.json()) as {
		beatgrid: { beats: AnlzBeat[]; status?: string; reason?: string | null };
	};
	const beats = payload.beatgrid.beats;
	test.skip(
		beats.length < 32,
		`${title} has no /anlz beatgrid (status ${payload.beatgrid.status ?? '?'}: ` +
			`${payload.beatgrid.reason ?? 'none'}); build the fixture with the own beatgrid ` +
			'producer: set MDT_BEATGRID_WEIGHTS (or PERFORMANCE_E2E_OWN_BEATGRID=1)'
	);
	return {
		stable_id: row!.stable_id,
		title,
		bpm: beats[Math.floor(beats.length / 2)].bpm,
		beatgrid_ms: beats.map((beat) => beat.t * 1000),
		beat_numbers: beats.map((beat) => beat.n)
	};
}

export async function waitForIpc(page: Page): Promise<void> {
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
}

export async function gotoPerformance(page: Page): Promise<void> {
	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto(`${UI_BASE}/performance`);
	await waitForIpc(page);
	await dispatch(page, { type: 'master_volume', value: 0.1 });
}

export async function query(page: Page): Promise<PerformanceState> {
	await waitForIpc(page);
	return page.evaluate(() => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.query();
	});
}

export async function dispatch(page: Page, command: PerformanceCommand): Promise<PerformanceState> {
	await waitForIpc(page);
	return page.evaluate(async (message) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.dispatch(message);
	}, command);
}

/** Wait for the deck to be loaded with a grid of at least 32 beats. */
export async function waitForGrid(page: Page, deck: DeckId): Promise<void> {
	await expect
		.poll(async () => (await query(page)).decks[deck].beatgrid_ms.length, {
			message: `deck ${deck} grid`
		})
		.toBeGreaterThan(31);
}

export async function waitAudible(page: Page, deck: DeckId): Promise<void> {
	await expect
		.poll(
			async () => {
				const state = await query(page);
				return state.decks[deck].audible && !state.decks[deck].transport_pending;
			},
			{ message: `deck ${deck} audible` }
		)
		.toBe(true);
}

/**
 * Signed phase difference, in master beats, between two decks right now,
 * read from ONE IPC snapshot (both positions come from the same query, so
 * the two decks are sampled at the same instant).
 *
 * Each deck's position is converted to a fractional beat on its own grid;
 * the follower's fraction is compared to the master's, wrapped to
 * (-0.5, 0.5]. `normalization` folds a half/double tempo pair: 0.5 means the
 * follower plays one beat per two master beats.
 */
export function beatFraction(deck: PerformanceDeckSnapshot): number {
	const grid = deck.beatgrid_ms;
	const pos = deck.position_ms;
	let i = 0;
	while (i + 1 < grid.length && grid[i + 1] <= pos) i += 1;
	const next = grid[Math.min(i + 1, grid.length - 1)];
	const span = next - grid[i];
	return span > 0 ? i + (pos - grid[i]) / span : i;
}

export function phaseErrorMs(
	master: PerformanceDeckSnapshot,
	follower: PerformanceDeckSnapshot,
	normalization = 1
): number {
	const masterBeats = beatFraction(master) * normalization;
	const followerBeats = beatFraction(follower);
	let diff = (followerBeats - masterBeats) % 1;
	if (diff > 0.5) diff -= 1;
	if (diff <= -0.5) diff += 1;
	const followerBeatMs = 60_000 / (follower.effective_bpm ?? follower.bpm ?? 120);
	return diff * followerBeatMs;
}

/** Median |phase error| over `samples` snapshots `gapMs` apart. */
export async function settledPhaseErrorMs(
	page: Page,
	master: DeckId,
	follower: DeckId,
	{ samples = 9, gapMs = 120, normalization = 1 } = {}
): Promise<number> {
	const errors: number[] = [];
	for (let i = 0; i < samples; i++) {
		const state = await query(page);
		errors.push(Math.abs(phaseErrorMs(state.decks[master], state.decks[follower], normalization)));
		await page.waitForTimeout(gapMs);
	}
	errors.sort((a, b) => a - b);
	return errors[Math.floor(errors.length / 2)];
}

/** Load two fixture tracks, make deck 1 master, sync deck 2, and play both. */
export async function playSyncedPair(
	page: Page,
	master: GridTrack,
	follower: GridTrack,
	{ masterBeat = 8, followerBeat = 20, beatSyncMax = true } = {}
): Promise<void> {
	await gotoPerformance(page);
	// BeatSyncMax defaults ON (prefs.svelte.ts). The TopBar button is the only
	// writer, so flip it the way a DJ would and confirm the pressed state.
	const bsm = page.locator('button.topbar-slot-bsm');
	if ((await bsm.getAttribute('aria-pressed')) !== String(beatSyncMax)) await bsm.click();
	await expect(bsm).toHaveAttribute('aria-pressed', String(beatSyncMax));
	await dispatch(page, { type: 'load', deck: 1, stable_id: master.stable_id });
	await dispatch(page, {
		type: 'load',
		deck: 2,
		stable_id: follower.stable_id
	});
	await waitForGrid(page, 1);
	await waitForGrid(page, 2);
	await dispatch(page, { type: 'master', deck: 1 });
	await dispatch(page, { type: 'beat_sync', deck: 2, enabled: true });
	await dispatch(page, {
		type: 'seek',
		deck: 1,
		position_ms: master.beatgrid_ms[masterBeat]
	});
	await dispatch(page, { type: 'play', deck: 1, playing: true });
	await waitAudible(page, 1);
	await dispatch(page, {
		type: 'seek',
		deck: 2,
		position_ms: follower.beatgrid_ms[followerBeat]
	});
	await dispatch(page, { type: 'play', deck: 2, playing: true });
	await waitAudible(page, 2);
	await expect
		.poll(async () => (await query(page)).decks[2].sync_error, {
			message: 'follower sync_error'
		})
		.toBeNull();
}

export interface SyncSample {
	/** ms since the action was dispatched (negative = before it). */
	t: number;
	/** Fractional beat index on each deck's own grid, null when unloaded. */
	beat: Record<number, number | null>;
	bpm: Record<number, number | null>;
	playing: Record<number, boolean>;
	/** AudioContext time (ms) each deck's position was presented at, null
	 * when paused. The rhythm reference: the context clock, not the wall
	 * clock, which keeps running through an output stall (xrun) that
	 * freezes every deck together. */
	ctxMs: Record<number, number | null>;
}

/**
 * Sample every deck's fractional beat position every ~`everyMs` from
 * `beforeMs` before the action to `afterMs` after it, all inside the page so
 * the timestamps and positions come from the same clock and the same IPC
 * snapshot. The action is an IPC command (or several) dispatched in-page.
 */
export async function sampleAround(
	page: Page,
	actions: PerformanceCommand[],
	{ beforeMs = 600, afterMs = 3000, everyMs = 20 } = {}
): Promise<{ samples: SyncSample[]; errors: string[] }> {
	return page.evaluate(
		async ({ actions, beforeMs, afterMs, everyMs }) => {
			const ipc = window.musicDjToolsPerformance!;
			const beatOf = (grid: number[], pos: number): number | null => {
				if (grid.length < 2) return null;
				let lo = 0;
				let hi = grid.length - 1;
				if (pos <= grid[0]) return (pos - grid[0]) / (grid[1] - grid[0]);
				if (pos >= grid[hi]) return hi + (pos - grid[hi]) / (grid[hi] - grid[hi - 1]);
				while (hi - lo > 1) {
					const mid = (lo + hi) >> 1;
					if (grid[mid] <= pos) lo = mid;
					else hi = mid;
				}
				return lo + (pos - grid[lo]) / (grid[lo + 1] - grid[lo]);
			};
			const samples: SyncSample[] = [];
			const errors: string[] = [];
			const take = (t0: number) => {
				const state = ipc.query();
				const sample: SyncSample = {
					t: performance.now() - t0,
					beat: {},
					bpm: {},
					playing: {},
					ctxMs: {}
				};
				for (const id of [1, 2, 3, 4]) {
					const deck = state.decks[id as 1 | 2 | 3 | 4];
					sample.beat[id] =
						deck.stable_id === null ? null : beatOf(deck.beatgrid_ms, deck.position_ms);
					sample.bpm[id] = deck.effective_bpm;
					sample.playing[id] = deck.playing;
					const ctx = deck.transport_clock.presentation_context_time_s;
					sample.ctxMs[id] = ctx === null ? null : ctx * 1000;
				}
				samples.push(sample);
			};
			const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
			const t0 = performance.now() + beforeMs;
			while (performance.now() < t0) {
				take(t0);
				await sleep(everyMs);
			}
			const dispatched = (async () => {
				for (const action of actions) {
					try {
						await ipc.dispatch(action);
					} catch (error) {
						errors.push(
							`${action.type}: ${error instanceof Error ? error.message : String(error)}`
						);
					}
				}
			})();
			while (performance.now() < t0 + afterMs) {
				take(t0);
				await sleep(everyMs);
			}
			await dispatched;
			return { samples, errors };
		},
		{ actions, beforeMs, afterMs, everyMs }
	);
}

function wrapHalf(value: number): number {
	let wrapped = value % 1;
	if (wrapped > 0.5) wrapped -= 1;
	if (wrapped <= -0.5) wrapped += 1;
	return wrapped;
}

/** Follower minus master phase, in follower ms, per sample (null when either
 * deck has no position). `normalization` 0.5 = follower at half tempo. */
export function phaseErrorsMs(
	samples: SyncSample[],
	master: number,
	follower: number,
	normalization = 1
): { t: number; err: number }[] {
	const out: { t: number; err: number }[] = [];
	for (const s of samples) {
		const m = s.beat[master];
		const f = s.beat[follower];
		const bpm = s.bpm[follower];
		if (m === null || f === null || bpm === null) continue;
		out.push({ t: s.t, err: wrapHalf(f - m * normalization) * (60_000 / bpm) });
	}
	return out;
}

/**
 * How far a deck's OWN rhythm moved, in ms of its own beat, between the last
 * sample before the action and the samples after `fromMs`: the beat position
 * after the action, compared modulo one beat with the beat position the deck
 * would have reached playing straight through, on the AudioContext clock.
 * 0 means the action kept the deck's beat phase (a quantized jump), anything
 * else is a skip a DJ hears.
 */
export function rhythmSlipMs(samples: SyncSample[], deck: number, fromMs: number): number[] {
	const before = samples.filter((s) => s.t < 0 && s.beat[deck] !== null && s.ctxMs[deck] !== null);
	const ref = before.at(-1);
	if (ref === undefined) throw new Error(`deck ${deck} has no pre-action sample`);
	const bpm = ref.bpm[deck]!;
	return samples
		.filter((s) => s.t >= fromMs && s.beat[deck] !== null && s.ctxMs[deck] !== null)
		.map((s) => {
			const expected = ref.beat[deck]! + ((s.ctxMs[deck]! - ref.ctxMs[deck]!) * bpm) / 60_000;
			return wrapHalf(s.beat[deck]! - expected) * (60_000 / bpm);
		});
}

export function maxAbs(values: number[]): number {
	return values.reduce((acc, value) => Math.max(acc, Math.abs(value)), 0);
}

/** Debug dump of a sampled window (env BEAT_SYNC_E2E_DUMP=1). */
export function dumpSamples(label: string, samples: SyncSample[]): void {
	if (process.env.BEAT_SYNC_E2E_DUMP !== '1') return;
	const rows = samples
		.filter((s, i) => i % 3 === 0 || (s.t > -150 && s.t < 700))
		.map(
			(s) =>
				`${s.t.toFixed(0)}\t${s.ctxMs[1]?.toFixed(0)}\t${s.beat[1]?.toFixed(4)}\t${s.beat[2]?.toFixed(4)}\t${s.bpm[1]?.toFixed(2)}\t${s.bpm[2]?.toFixed(2)}`
		);
	console.log(`--- ${label}\nt\tctx1\tbeat1\tbeat2\tbpm1\tbpm2\n${rows.join('\n')}`);
}
