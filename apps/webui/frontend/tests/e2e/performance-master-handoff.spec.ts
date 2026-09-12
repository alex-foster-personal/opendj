import { expect, test } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { beatgridFor } from './support/analyzed-track';
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
	return [tracks[0]!, tracks[1]!];
}

async function _loadPair(page: Page, master: RealTrack, follower: RealTrack): Promise<void> {
	await _dispatch(page, { type: 'load', deck: 1, stable_id: master.stable_id });
	await _dispatch(page, { type: 'load', deck: 2, stable_id: follower.stable_id });
}

test.describe('Beat Sync MASTER handoff (DECKUX-17)', () => {
	test('AUTO hands master to a playing follower when deck 1 pauses', async ({ page, request }) => {
		const [masterTrack, followerTrack] = await _realSyncPair(request);
		await _gotoPerformance(page);
		await _loadPair(page, masterTrack, followerTrack);
		await _dispatch(page, { type: 'play', deck: 1, playing: true });
		await _dispatch(page, { type: 'play', deck: 2, playing: true });
		await _dispatch(page, { type: 'beat_sync', deck: 2, enabled: true });
		await expect.poll(async () => (await _query(page)).master_deck).toBe(1);
		await _dispatch(page, { type: 'play', deck: 1, playing: false });
		const state = await _idleState(page);
		expect(state.master_deck).toBe(2);
		expect(state.master_mode).toBe('auto');
	});

	test('unload master elects the remaining playing deck', async ({ page, request }) => {
		const [masterTrack, followerTrack] = await _realSyncPair(request);
		await _gotoPerformance(page);
		await _loadPair(page, masterTrack, followerTrack);
		await _dispatch(page, { type: 'play', deck: 1, playing: true });
		await _dispatch(page, { type: 'play', deck: 2, playing: true });
		await _dispatch(page, { type: 'unload', deck: 1 });
		const state = await _idleState(page);
		expect(state.master_deck).toBe(2);
	});

	test('natural end hands master to the on-air follower', async ({ page, request }) => {
		const [masterTrack, followerTrack] = await _realSyncPair(request);
		await _gotoPerformance(page);
		await _loadPair(page, masterTrack, followerTrack);
		await _dispatch(page, { type: 'play', deck: 1, playing: true });
		await _dispatch(page, { type: 'play', deck: 2, playing: true });
		const durationMs = (await _query(page)).decks[1].duration_ms;
		if (durationMs === null) throw new Error('loaded real track has no decoded duration');
		await _dispatch(page, {
			type: 'seek',
			deck: 1,
			position_ms: Math.max(0, durationMs - 250)
		});
		await expect
			.poll(async () => {
				const deck = (await _query(page)).decks[1];
				return !deck.playing && !deck.audible && !deck.transport_pending;
			})
			.toBe(true);
		const state = await _idleState(page);
		expect(state.master_deck).toBe(2);
	});

	test('fader-down cue deck does not steal master from an on-air deck', async ({ page, request }) => {
		const [masterTrack, followerTrack] = await _realSyncPair(request);
		await _gotoPerformance(page);
		await _loadPair(page, masterTrack, followerTrack);
		await _dispatch(page, { type: 'play', deck: 2, playing: true });
		await _dispatch(page, { type: 'master', deck: 2 });
		await _dispatch(page, { type: 'fader', deck: 1, value: 0 });
		await _dispatch(page, { type: 'play', deck: 1, playing: true });
		const state = await _idleState(page);
		expect(state.master_deck).toBe(2);
	});

	test('locked master survives another deck starting play until unlock', async ({ page, request }) => {
		const [masterTrack, followerTrack] = await _realSyncPair(request);
		await _gotoPerformance(page);
		await _loadPair(page, masterTrack, followerTrack);
		await _dispatch(page, { type: 'play', deck: 1, playing: true });
		await _dispatch(page, { type: 'master', deck: 1, lock: true });
		await _dispatch(page, { type: 'play', deck: 1, playing: false });
		await _dispatch(page, { type: 'play', deck: 2, playing: true });
		let state = await _idleState(page);
		expect(state.master_deck).toBe(1);
		expect(state.master_mode).toBe('locked');
		state = await _dispatch(page, { type: 'master', deck: 1, lock: false });
		expect(state.master_mode).toBe('auto');
		expect(state.master_deck).toBe(2);
	});

	test('MASTER button exposes auto and locked data-state', async ({ page, request }) => {
		const [masterTrack] = await _realSyncPair(request);
		await _gotoPerformance(page);
		await _dispatch(page, { type: 'load', deck: 1, stable_id: masterTrack.stable_id });
		await _dispatch(page, { type: 'play', deck: 1, playing: true });
		const masterButton = page.locator('[data-testid="master-deck-1"]');
		await expect(masterButton).toHaveAttribute('data-state', 'auto');
		await masterButton.click();
		await expect(masterButton).toHaveAttribute('data-state', 'locked');
		await masterButton.click();
		await expect(masterButton).toHaveAttribute('data-state', 'auto');
	});
});
