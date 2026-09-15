import { expect, test } from '@playwright/test';
import { readFileSync } from 'node:fs';
import {
	advanceBeatStamp,
	msToBeatPosition
} from '../../src/lib/rb/performance-rescue-math';
import type { PerformanceBrowserIpc } from '../../src/lib/rb/performance-ipc.svelte';
import { FIXTURE_MANIFEST_PATH } from './playwright.performance.config';

const FIXTURE_MODE = process.env.PERFORMANCE_E2E_FIXTURE !== '0';

interface ManifestTrack {
	stable_id: string;
	title: string;
}

interface FixtureManifest {
	tracks: ManifestTrack[];
}

function readManifest(): FixtureManifest {
	return JSON.parse(readFileSync(FIXTURE_MANIFEST_PATH, 'utf8')) as FixtureManifest;
}

interface PerfEventRow {
	kind: string;
	deck: number | null;
	stages?: Record<string, number>;
}

/** Stage key for the post-clamp schedule time the LATENCY-03 ring stores. */
const RESCUE_SCHEDULE_TIME_STAGE = 'scheduled_offset_ms' as const;

async function dispatch(
	page: import('@playwright/test').Page,
	command: Record<string, unknown>
): Promise<unknown> {
	return page.evaluate(async (payload) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.dispatch(payload);
	}, command);
}

type PerformanceQueryResult = Awaited<ReturnType<PerformanceBrowserIpc['query']>>;

async function query(page: import('@playwright/test').Page): Promise<PerformanceQueryResult> {
	return page.evaluate(async () => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.query();
	});
}

test.describe('RESCUE-02/03 playback restore', () => {
	test.skip(!FIXTURE_MODE, 'requires PERFORMANCE_E2E_FIXTURE rescue-playback seed');

	test('reload resumes playing decks together from beat stamps', async ({ page }) => {
		const manifest = readManifest();
		const deckTracks = manifest.tracks.slice(0, 3);
		const deckIds = [1, 3, 4] as const;

		await page.goto('/performance');
		await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

		for (let index = 0; index < deckIds.length; index += 1) {
			const deck = deckIds[index];
			const stable_id = deckTracks[index].stable_id;
			await dispatch(page, { type: 'load', deck, stable_id });
			await dispatch(page, { type: 'play', deck, playing: true });
		}

		await page.waitForTimeout(1_500);
		const before = await query(page);
		const capturedAt = Date.now() - 2_000;
		const recordedBeatStamp = {
			kind: 'beatgrid' as const,
			beat_index: 16,
			beat_n: 1,
			phase: 0
		};

		const rescueSnapshot = {
			schema: 1,
			captured_at_ms: capturedAt,
			reason: 'transport',
			app_posture: 'gig',
			master_deck: null,
			playlist_id: null,
			mixer: {
				crossfader: before.mixer.crossfader,
				master: before.mixer.master,
				headphones: before.mixer.headphones
			},
			decks: Object.fromEntries(
				([1, 2, 3, 4] as const).map((deckId) => {
					const deck = before.decks[deckId];
					const playing = deckIds.includes(deckId as 1 | 3 | 4);
					return [
						deckId,
						{
							deck_id: deckId,
							stable_id: deck.stable_id,
							source_path: null,
							position_ms: deck.position_ms,
							playing,
							beat_stamp: recordedBeatStamp,
							pitch: deck.pitch,
							pitch_range: deck.pitch_range,
							master_tempo_enabled: deck.master_tempo_enabled,
							key_sync_enabled: deck.key_sync_enabled,
							quantize_enabled: deck.quantize_enabled,
							beat_sync_enabled: deck.beat_sync_enabled,
							sync_mode: deck.sync_mode,
							is_master: deck.is_master,
							cue_ms: deck.cue_ms,
							loop: deck.loop,
							hot_cue_armed: deck.hot_cue_armed,
							stems: {
								vocal: { ...deck.stems.controls.vocal },
								instrumental: { ...deck.stems.controls.instrumental },
								drums: { ...deck.stems.controls.drums }
							},
							mixer_channel: {
								trim: before.mixer.channels[deckId].trim,
								eq_high: before.mixer.channels[deckId].eq_high,
								eq_mid: before.mixer.channels[deckId].eq_mid,
								eq_low: before.mixer.channels[deckId].eq_low,
								filter: before.mixer.channels[deckId].filter,
								fader: before.mixer.channels[deckId].fader,
								assign: before.mixer.channels[deckId].assign,
								cue_enabled: before.mixer.channels[deckId].cue_enabled
							}
						}
					];
				})
			)
		};

		await page.route('**/api/v1/performance/rescue-snapshots/latest', async (route) => {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify(rescueSnapshot)
			});
		});

		await page.reload();
		await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

		await expect
			.poll(async () => {
				const state = await query(page);
				return deckIds.map((deckId) => state.decks[deckId].playing);
			})
			.toEqual([true, true, true]);

		const perfRows = await page.evaluate(() => {
			const read = (window as Window & { __mdtPerfLog?: () => readonly PerfEventRow[] }).__mdtPerfLog;
			if (read === undefined) {
				throw new Error('__mdtPerfLog is not installed; the latency instrument is missing');
			}
			return read();
		});

		const scheduleRows = perfRows.filter(
			(row) =>
				row.kind === 'transport-schedule' &&
				row.deck !== null &&
				deckIds.includes(row.deck as (typeof deckIds)[number])
		);

		const times = scheduleRows
			.map((row) => row.stages?.[RESCUE_SCHEDULE_TIME_STAGE])
			.filter((value): value is number => value !== undefined);

		expect(
			times.length,
			'every restored deck must log one timed transport-schedule row; fewer means a deck was not scheduled on rescue resume'
		).toBe(deckIds.length);

		const skewMs = Math.max(...times) - Math.min(...times);
		expect(skewMs).toBeLessThan(5);

		const after = await query(page);
		const elapsedWallMs = Date.now() - capturedAt;
		const expectedBeat = advanceBeatStamp(recordedBeatStamp, GRID_MS_FROM(before), 1, elapsedWallMs);
		for (const deckId of deckIds) {
			const deck = after.decks[deckId];
			const actualBeat = msToBeatPosition(deck.beatgrid_ms, deck.position_ms);
			expect(actualBeat).not.toBeNull();
			expect(Math.abs(actualBeat! - expectedBeat)).toBeLessThan(0.125);
		}
	});

	test('missing rescue snapshot does not auto-play after reload', async ({ page }) => {
		await page.route('**/api/v1/performance/rescue-snapshots/latest', async (route) => {
			await route.fulfill({ status: 404, body: '' });
		});
		await page.goto('/performance');
		await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
		await page.reload();
		await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
		const state = await query(page);
		expect(([1, 2, 3, 4] as const).every((deckId) => state.decks[deckId].playing === false)).toBe(true);
	});
});

function GRID_MS_FROM(before: { decks: Record<number, { beatgrid_ms: number[] }> }): number[] {
	return before.decks[1].beatgrid_ms;
}
