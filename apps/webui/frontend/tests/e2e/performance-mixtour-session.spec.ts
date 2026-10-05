import { expect, test, type Page } from '@playwright/test';
import type { PerformanceCommand } from '../../src/lib/rb/performance-ipc.svelte';

const DECKS = [1, 2, 3, 4] as const;
const TRACK = process.env.PERFORMANCE_E2E_MIXTOUR_TRACK;

// Supersedes fabricated dispatch/query restore cases in unit/rescue-restore.test.mjs.

async function dispatch(page: Page, command: PerformanceCommand) {
	return page.evaluate((command) => window.musicDjToolsPerformance!.dispatch(command), command);
}

test('Mixtour stem settings survive real four-deck session and rescue restore', async ({ page }, info) => {
	test.setTimeout(180_000);
	if (!TRACK) throw new Error('Set PERFORMANCE_E2E_MIXTOUR_TRACK to a real analyzed Demucs track');
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await dispatch(page, { type: 'master_volume', value: 0.1 });
	for (const deck of DECKS) {
		await dispatch(page, { type: 'load', deck, stable_id: TRACK });
		await page.waitForFunction((deck) => window.musicDjToolsPerformance!.query().decks[deck].stems.status === 'ready', deck);
		await dispatch(page, { type: 'stem_mute', deck, stem: 'bass', muted: true });
		await dispatch(page, { type: 'stem_gain', deck, stem: 'other', value: 0.3 });
		await dispatch(page, { type: 'stem_solo', deck, stem: 'drums', solo: true });
	}
	const before = await page.evaluate(async (url) => {
		const { flushPerformanceSessionSnapshot } = await import(url) as typeof import('../../src/lib/rb/performance-session.svelte');
		flushPerformanceSessionSnapshot();
		return window.musicDjToolsPerformance!.query();
	}, '/src/lib/rb/performance-session.svelte.ts');
	await page.reload();
	await page.waitForFunction(() => {
		const state = window.musicDjToolsPerformance?.query();
		return state && [1, 2, 3, 4].every((deck) => state.decks[deck as 1 | 2 | 3 | 4].stems.status === 'ready');
	}, undefined, { timeout: 60_000 });
	await expect.poll(() => page.evaluate(() => window.musicDjToolsPerformance!.query().command_pending)).toBe(false);
	await expect.poll(() => page.evaluate(() => {
		const state = window.musicDjToolsPerformance!.query();
		return [1, 2, 3, 4].map((deck) => state.decks[deck as 1 | 2 | 3 | 4].stems.controls);
	})).toEqual(DECKS.map((deck) => before.decks[deck].stems.controls));
	const after = await page.evaluate(() => window.musicDjToolsPerformance!.query());
	await info.attach('reload-state', { body: JSON.stringify({ before, after }), contentType: 'application/json' });
	expect(after.last_error).toBeNull();
	for (const deck of DECKS) {
		expect(after.decks[deck].stems.controls).toEqual(before.decks[deck].stems.controls);
		expect(after.decks[deck].playing).toBe(false);
		expect(after.decks[deck].command_error).toBeNull();
	}
	const rescue = await page.evaluate(async ({ snapshotUrl, restoreUrl }) => {
		const { buildRescueSnapshot } = await import(snapshotUrl) as typeof import('../../src/lib/rb/rescue-snapshot');
		const { executeRescueRestore } = await import(restoreUrl) as typeof import('../../src/lib/rb/rescue-restore.svelte');
		const snapshot = buildRescueSnapshot(window.musicDjToolsPerformance!.query(), 'periodic', Date.now(),
			{ 1: null, 2: null, 3: null, 4: null });
		const result = await executeRescueRestore({ snapshot, mode: 'layout' });
		return { result, state: window.musicDjToolsPerformance!.query() };
	}, { snapshotUrl: '/src/lib/rb/rescue-snapshot.ts', restoreUrl: '/src/lib/rb/rescue-restore.svelte.ts' });
	await info.attach('rescue-state', { body: JSON.stringify(rescue), contentType: 'application/json' });
	expect(rescue.state.last_error).toBeNull();
	for (const deck of DECKS) {
		expect(rescue.result.decks[String(deck)].outcome).toBe('paused');
		expect(rescue.state.decks[deck].stems.controls).toEqual(before.decks[deck].stems.controls);
	}
	for (const deck of [1, 3] as const) await dispatch(page, { type: 'play', deck, playing: true });
	await page.waitForFunction(() => {
		const decks = window.musicDjToolsPerformance!.query().decks;
		return decks[1].audible && decks[3].audible;
	});
	const resumed = await page.evaluate(async ({ snapshotUrl, restoreUrl }) => {
		const { buildRescueSnapshot } = await import(snapshotUrl) as typeof import('../../src/lib/rb/rescue-snapshot');
		const { executeRescueRestore } = await import(restoreUrl) as typeof import('../../src/lib/rb/rescue-restore.svelte');
		const ipc = window.musicDjToolsPerformance!;
		const snapshot = buildRescueSnapshot(ipc.query(), 'periodic', Date.now(), { 1: null, 2: null, 3: null, 4: null });
		for (const deck of [1, 3] as const) await ipc.dispatch({ type: 'play', deck, playing: false });
		return executeRescueRestore({ snapshot, mode: 'play' });
	}, { snapshotUrl: '/src/lib/rb/rescue-snapshot.ts', restoreUrl: '/src/lib/rb/rescue-restore.svelte.ts' });
	await page.waitForFunction(() => {
		const decks = window.musicDjToolsPerformance!.query().decks;
		return decks[1].audible && decks[3].audible && !decks[2].playing && !decks[4].playing;
	});
	for (const deck of [1, 3]) expect(resumed.decks[String(deck)].outcome).toBe('resumed');
	const played = await page.evaluate(() => window.musicDjToolsPerformance!.query());
	expect(played.last_error).toBeNull();
	for (const deck of DECKS) {
		expect(played.decks[deck].stems.controls).toEqual(before.decks[deck].stems.controls);
		await dispatch(page, { type: 'play', deck, playing: false });
		await dispatch(page, { type: 'unload', deck });
	}
});
