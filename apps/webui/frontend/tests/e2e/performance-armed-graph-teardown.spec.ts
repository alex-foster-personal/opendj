/**
 * Armed hot-cue countdown reads after REAL graph lifecycle events (issue #6680).
 *
 * `armed-snapshot-purity.test.mjs` proves the guarded-$derived rule under node
 * with the documented hot-cue driver seam; it cannot build a browser
 * AudioContext. This spec is where teardown/read ordering is proved on the
 * production graph Chromium actually runs.
 *
 * Acceptance:
 * - [if] BeatSyncMax arms a hot cue on a playing deck [then] query reports it armed.
 * - [if] production `engine.dispose()` runs before the arm lands [then] a query
 *   reads hot_cue_armed as null without throwing (AGENTS.md: no fabricated graph).
 * - [if] production `drillGraphRebuild` runs before the arm lands [then] a query
 *   reads hot_cue_armed as null on the rebuilt graph clock (not only full dispose).
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import {
	API_BASE,
	dispatch,
	gotoPerformance,
	ownGridTrack,
	query,
	waitAudible,
	waitForGrid
} from './support/beat-sync-e2e';

const TRACK_TITLE = 'webkit-fixture-a-128bpm';
const ENGINE_MODULE = '/src/lib/rb/audio-engine.svelte.ts';
const HOT_CUE_SLOT = 'A';
const HOT_CUE_BEAT_INDEX = 20;

const FIXTURE_MODE = process.env.PERFORMANCE_E2E_FIXTURE !== '0';

async function hotCueSlotRevision(
	request: APIRequestContext,
	stableId: string,
	slot: string
): Promise<string> {
	const response = await request.get(
		`${API_BASE}/api/v1/tracks/${encodeURIComponent(stableId)}/hot-cues`
	);
	expect(response.ok(), `hot-cues for ${stableId}`).toBeTruthy();
	const slots = (await response.json()) as { slot: string; revision: string }[];
	const row = slots.find((entry) => entry.slot === slot);
	if (row === undefined) throw new Error(`hot cue slot ${slot} missing from library`);
	return row.revision;
}

/** Production load/play/save/trigger; returns once hot_cue_armed is live on deck 1. */
async function armHotCueOnPlayingDeck(
	page: Page,
	request: APIRequestContext
): Promise<{ stableId: string; armedSlot: string }> {
	const track = await ownGridTrack(request, TRACK_TITLE);
	const cueInMs = track.beatgrid_ms[Math.min(HOT_CUE_BEAT_INDEX, track.beatgrid_ms.length - 1)];

	await gotoPerformance(page);
	await dispatch(page, { type: 'load', deck: 1, stable_id: track.stable_id });
	await waitForGrid(page, 1);
	await dispatch(page, { type: 'play', deck: 1, playing: true });
	await waitAudible(page, 1);

	const revision = await hotCueSlotRevision(request, track.stable_id, HOT_CUE_SLOT);
	await dispatch(page, {
		type: 'hot_cue_save',
		deck: 1,
		slot: HOT_CUE_SLOT,
		in_ms: cueInMs,
		revision
	});

	const savedCue = (await query(page)).decks[1].hot_cue_slots.find(
		(slot) => slot.slot === HOT_CUE_SLOT
	)?.cue;
	expect(savedCue, 'production hot_cue_save must leave slot A populated').not.toBeNull();

	await dispatch(page, { type: 'hot_cue_trigger', deck: 1, slot: HOT_CUE_SLOT });

	let armedSlot: string | null = null;
	await expect
		.poll(
			async () => {
				const armed = (await query(page)).decks[1].hot_cue_armed;
				if (armed === null || armed.remaining_ms <= 0) return null;
				armedSlot = armed.slot;
				return armed;
			},
			{ message: 'hot_cue_trigger must arm deck 1 on the production graph' }
		)
		.not.toBeNull();

	expect(armedSlot).toBe(HOT_CUE_SLOT);
	return { stableId: track.stable_id, armedSlot: armedSlot! };
}

test.use({
	launchOptions: { args: ['--autoplay-policy=no-user-gesture-required', '--mute-audio'] }
});

test('a hot cue arm reads as null after the real audio graph is disposed, without throwing', async ({
	page,
	request
}) => {
	test.skip(!FIXTURE_MODE, 'requires the performance Playwright fixture library');

	await armHotCueOnPlayingDeck(page, request);

	const afterDispose = await page.evaluate(async (engineModuleUrl) => {
		const audio = (await import(engineModuleUrl)) as {
			engine: { dispose(): Promise<void> };
		};
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		await audio.engine.dispose();
		return ipc.query().decks[1].hot_cue_armed;
	}, ENGINE_MODULE);

	expect(afterDispose).toBeNull();
});

test('a hot cue arm reads as null after drillGraphRebuild on the real browser graph', async ({
	page,
	request
}) => {
	test.skip(!FIXTURE_MODE, 'requires the performance Playwright fixture library');

	await armHotCueOnPlayingDeck(page, request);

	const afterRebuild = await page.evaluate(async () => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		const result = await ipc.drillGraphRebuild({ fail_add_module: 0 });
		return {
			error: result.error,
			reattaching: result.reattaching,
			hotCueArmed: result.state.decks[1].hot_cue_armed
		};
	});

	expect(afterRebuild.error, 'graph rebuild must succeed on the armed deck').toBeNull();
	expect(afterRebuild.hotCueArmed).toBeNull();
	expect(afterRebuild.reattaching).toEqual([]);
});
