import { expect, test } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { beatgridFor } from './support/analyzed-track';
import { REANCHOR_RAMP_DURATION_SEC } from '../../src/lib/rb/beat-sync-math';
import type { PerformanceCommand, PerformanceState } from '../../src/lib/rb/performance-ipc.svelte';
import type { DeckId } from '../../src/lib/rb/deck-slots';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const DATA_DIR = process.env.MDT_DATA_DIR ?? join(REPOSITORY_ROOT, 'data');
const IS_GENERATED_FIXTURE = existsSync(join(DATA_DIR, 'fixture-revision.txt'));
const API_BASE = process.env.PERFORMANCE_E2E_API_BASE ?? 'http://127.0.0.1:8686';
const UI_BASE = (
	process.env.PERFORMANCE_E2E_BASE_URL ??
	process.env.PERFORMANCE_E2E_UI_BASE ??
	''
).replace(/\/$/, '');
const SYNC_MASTER_RATE = 1.1;
const RAMP_SETTLE_MS = Math.ceil(REANCHOR_RAMP_DURATION_SEC * 1000) + 150;
const E2E_MASTER_VOLUME = 0.1;

interface TrackWire {
	stable_id: string;
	bpm: number | null;
	file_exists: boolean;
}

interface RealTrack {
	stable_id: string;
	bpm: number;
	beatgrid_ms: number[];
}

let analyzedTracks: RealTrack[] | null = null;

async function _waitForIpc(page: Page): Promise<void> {
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
}

async function _gotoPerformance(page: Page): Promise<void> {
	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto(`${UI_BASE}/performance`);
	await _waitForIpc(page);
	await _dispatch(page, { type: 'master_volume', value: E2E_MASTER_VOLUME });
}

async function _query(page: Page): Promise<PerformanceState> {
	await _waitForIpc(page);
	return page.evaluate(() => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.query();
	});
}

async function _dispatch(page: Page, command: PerformanceCommand): Promise<PerformanceState> {
	await _waitForIpc(page);
	return page.evaluate(async (message) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.dispatch(message);
	}, command);
}

async function _idleState(page: Page): Promise<PerformanceState> {
	await _waitForIpc(page);
	await page.waitForFunction(() => window.musicDjToolsPerformance?.query().command_pending === false);
	return _query(page);
}

async function _firstPresentedTransportState(
	page: Page,
	deck: DeckId,
	audible: boolean
): Promise<PerformanceState> {
	await _waitForIpc(page);
	return page.evaluate(
		async ({ deckId, expectedAudible }) => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			const deadline = performance.now() + 15_000;
			let lastDeck = ipc.query().decks[deckId];
			while (performance.now() < deadline) {
				const state = ipc.query();
				const candidate = state.decks[deckId];
				lastDeck = candidate;
				if (
					candidate.audible === expectedAudible &&
					!candidate.transport_pending &&
					candidate.transport_clock.presented_revision ===
						candidate.transport_clock.desired_revision
				) {
					return state;
				}
				await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
			}
			throw new Error(
				`deck ${deckId} did not reach presented audible=${String(expectedAudible)} state; ` +
					`last deck state=${JSON.stringify(lastDeck)}`
			);
		},
		{ deckId: deck, expectedAudible: audible }
	);
}

async function _fetchAnalyzedTracks(request: APIRequestContext): Promise<RealTrack[]> {
	test.skip(
		IS_GENERATED_FIXTURE,
		`generated fixture at ${DATA_DIR}: run with PERFORMANCE_E2E_FIXTURE=0 against an analyzed library`
	);
	if (analyzedTracks !== null) return analyzedTracks;
	const tracksResponse = await request.get(`${API_BASE}/api/v1/tracks?limit=1000&available=true`);
	expect(tracksResponse.ok()).toBeTruthy();
	const payload = (await tracksResponse.json()) as { items: TrackWire[] };
	const candidates = payload.items.filter(
		(track): track is TrackWire & { bpm: number } =>
			track.file_exists && typeof track.bpm === 'number' && track.bpm > 0
	);
	test.skip(candidates.length <= 1, 'requires a real library with at least two BPM-tagged tracks');
	const found: RealTrack[] = [];
	for (const track of candidates) {
		if (found.length >= 8) break;
		const anlz = await beatgridFor(request, API_BASE, track.stable_id, []);
		if (anlz === null) continue;
		const beatgrid_ms = anlz.beatgrid.beats.map((beat) => beat.t * 1000);
		if (beatgrid_ms.length < 32) continue;
		found.push({ stable_id: track.stable_id, bpm: track.bpm, beatgrid_ms });
	}
	test.skip(found.length < 2, 'no analyzed sync pair in the real library');
	analyzedTracks = found;
	return found;
}

async function _realSyncPair(request: APIRequestContext): Promise<[RealTrack, RealTrack]> {
	const tracks = await _fetchAnalyzedTracks(request);
	for (const master of tracks) {
		for (const follower of tracks) {
			if (master.stable_id === follower.stable_id) continue;
			const ratio = master.bpm / follower.bpm;
			const changedMasterRatio = ratio * SYNC_MASTER_RATE;
			const materiallyDifferent = Math.abs(ratio - 1) >= 0.005;
			const initialRatioFits = ratio >= 0.84 && ratio <= 1.16;
			const changedRatioFits = changedMasterRatio >= 0.84 && changedMasterRatio <= 1.16;
			if (materiallyDifferent && initialRatioFits && changedRatioFits) {
				return [master, follower];
			}
		}
	}
	throw new Error(
		`real library has no distinct-BPM sync pair within +-16% before and after ${SYNC_MASTER_RATE}x master rate`
	);
}

function _phaseDeltaMs(state: PerformanceState): number | null {
	const master = state.decks[1];
	const follower = state.decks[2];
	const nextMaster = master.beatgrid_ms.find((beat) => beat > master.position_ms + 0.5);
	const nextFollower = follower.beatgrid_ms.find((beat) => beat > follower.position_ms + 0.5);
	if (nextMaster === undefined || nextFollower === undefined) return null;
	return Math.abs(
		(nextMaster - master.position_ms) / master.pitch -
			(nextFollower - follower.position_ms) / follower.pitch
	);
}

async function _syncedPlayingPair(
	page: Page,
	request: APIRequestContext
): Promise<{ masterTrack: RealTrack; followerTrack: RealTrack }> {
	const [masterTrack, followerTrack] = await _realSyncPair(request);
	await _gotoPerformance(page);
	await _dispatch(page, { type: 'load', deck: 1, stable_id: masterTrack.stable_id });
	await _dispatch(page, { type: 'load', deck: 2, stable_id: followerTrack.stable_id });
	await _dispatch(page, { type: 'master', deck: 1 });
	await _dispatch(page, { type: 'beat_sync', deck: 2, enabled: true });
	await _dispatch(page, { type: 'play', deck: 1, playing: true });
	await _dispatch(page, { type: 'play', deck: 2, playing: true });
	await expect
		.poll(async () => {
			const active = await _query(page);
			return (
				active.decks[1].audible &&
				active.decks[2].audible &&
				!active.decks[1].transport_pending &&
				!active.decks[2].transport_pending
			);
		})
		.toBe(true);
	return { masterTrack, followerTrack };
}

test.describe('stale re-anchor ramp supersession @performance-stale-reanchor', () => {
	test('pause during master re-anchor leaves follower stopped after ramp window', async ({
		page,
		request
	}) => {
		await _syncedPlayingPair(page, request);
		const crossDeck = await page.evaluate(async (masterRate) => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			const masterRatePromise = ipc.dispatch({ type: 'tempo', deck: 1, ratio: masterRate });
			const followerPause = ipc.dispatch({ type: 'play', deck: 2, playing: false });
			await Promise.all([masterRatePromise, followerPause]);
			return ipc.query();
		}, SYNC_MASTER_RATE);
		expect(crossDeck.decks[2].playing).toBe(false);
		await page.waitForTimeout(RAMP_SETTLE_MS);
		const settled = await _firstPresentedTransportState(page, 2, false);
		expect(settled.decks[2].playing).toBe(false);
		expect(settled.decks[2].audible).toBe(false);
		expect(settled.decks[2].transport_pending).toBe(false);
		expect(settled.decks[2].transport_clock.presented_revision).toBe(
			settled.decks[2].transport_clock.desired_revision
		);
		expect(settled.decks[2].sync_error).toBeNull();
	});

	test('overlapping re-anchors settle on the latest master ratio', async ({ page, request }) => {
		await _syncedPlayingPair(page, request);
		const overlap = await page.evaluate(async (masterRate) => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			const settledPhaseDeltasMs: number[] = [];

			function samplePhaseDelta(): void {
				const state = ipc?.query();
				if (state === undefined) throw new Error('performance IPC disappeared during overlap');
				const master = state.decks[1];
				const follower = state.decks[2];
				const nextMaster = master.beatgrid_ms.find((beat) => beat > master.position_ms + 0.5);
				const nextFollower = follower.beatgrid_ms.find(
					(beat) => beat > follower.position_ms + 0.5
				);
				if (nextMaster === undefined || nextFollower === undefined) return;
				settledPhaseDeltasMs.push(
					Math.abs(
						(nextMaster - master.position_ms) / master.pitch -
							(nextFollower - follower.position_ms) / follower.pitch
					)
				);
			}

			void ipc.dispatch({ type: 'tempo', deck: 1, ratio: 1.05 });
			await ipc.dispatch({ type: 'tempo', deck: 1, ratio: masterRate });
			const deadline = performance.now() + 5_000;
			while (performance.now() < deadline) {
				const state = ipc.query();
				samplePhaseDelta();
				if (!state.decks[1].transport_pending && !state.decks[2].transport_pending) break;
				await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
			}
			await new Promise<void>((resolve) => window.setTimeout(resolve, 80));
			samplePhaseDelta();
			return { settled_phase_deltas_ms: settledPhaseDeltasMs, after: ipc.query() };
		}, SYNC_MASTER_RATE);
		expect(overlap.settled_phase_deltas_ms.length).toBeGreaterThan(0);
		expect(Math.max(...overlap.settled_phase_deltas_ms)).toBeLessThan(30);
		const state = overlap.after;
		expect(state.decks[2].transport_pending).toBe(false);
		expect(state.decks[1].pitch).toBeCloseTo(SYNC_MASTER_RATE, 4);
		expect(state.decks[1].effective_bpm).not.toBeNull();
		expect(state.decks[2].effective_bpm).not.toBeNull();
		expect(
			Math.abs((state.decks[1].effective_bpm ?? 0) - (state.decks[2].effective_bpm ?? 0))
		).toBeLessThan(0.02);
	});

	test('beat sync disable during ramp stops further tempo writes', async ({ page, request }) => {
		await _syncedPlayingPair(page, request);
		const samples = await page.evaluate(async (masterRate) => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			const collected: number[] = [];
			const timer = window.setInterval(() => {
				collected.push(ipc.query().decks[2].pitch);
			}, 20);
			void ipc.dispatch({ type: 'tempo', deck: 1, ratio: masterRate });
			await new Promise<void>((resolve) => window.setTimeout(resolve, 40));
			await ipc.dispatch({ type: 'beat_sync', deck: 2, enabled: false });
			await new Promise<void>((resolve) => window.setTimeout(resolve, 350));
			window.clearInterval(timer);
			return collected;
		}, SYNC_MASTER_RATE);
		const unique = [...new Set(samples.map((pitch) => pitch.toFixed(5)))];
		expect(unique.length).toBeGreaterThan(0);
		const tail = samples.slice(-5);
		expect(tail.every((pitch) => pitch === tail[0])).toBe(true);
	});

	test('promoting a ramping follower to master does not let the old tail drive tempo', async ({
		page,
		request
	}) => {
		await _syncedPlayingPair(page, request);
		await _dispatch(page, { type: 'beat_sync', deck: 1, enabled: true });
		await page.evaluate(async (masterRate) => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			void ipc.dispatch({ type: 'tempo', deck: 1, ratio: masterRate });
			await new Promise<void>((resolve) => window.setTimeout(resolve, 30));
			await ipc.dispatch({ type: 'master', deck: 2 });
		}, SYNC_MASTER_RATE);
		await page.waitForTimeout(RAMP_SETTLE_MS);
		const state = await _idleState(page);
		expect(state.master_deck).toBe(2);
		expect(state.decks[2].transport_pending).toBe(false);
		expect(state.decks[2].sync_error).toBeNull();
		expect(state.decks[2].pitch).toBeCloseTo(SYNC_MASTER_RATE, 4);
		expect(state.decks[1].beat_sync_enabled).toBe(true);
		expect(state.decks[1].transport_pending).toBe(false);
		expect(state.decks[1].sync_error).toBeNull();
		expect(state.decks[1].effective_bpm).not.toBeNull();
		expect(state.decks[2].effective_bpm).not.toBeNull();
		expect(
			Math.abs((state.decks[1].effective_bpm ?? 0) - (state.decks[2].effective_bpm ?? 0))
		).toBeLessThan(0.02);
		const phaseDelta = _phaseDeltaMs(state);
		expect(phaseDelta).not.toBeNull();
		expect(phaseDelta!).toBeLessThan(30);
	});
});
