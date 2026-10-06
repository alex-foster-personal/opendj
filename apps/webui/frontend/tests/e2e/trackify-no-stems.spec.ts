// requirement: PERFMODE-15
// [if] Trackify loads and plays a track [then] no stem request leaves the page and the deck reads
//   stems unavailable naming Trackify [⛔️ if the request counter cannot see a stem request at all]
//
// Real engine, API and decoder end to end (Codex review, PR #4039). The unit layer
// (tests/unit/trackify-no-stems.test.mjs) owns the Gig control, in-flight cancellation and the
// decks Trackify unloads on mount; this spec proves the Trackify half on the real stack.

import { expect, test } from '@playwright/test';

test('Trackify loads a track without any stem request, and its deck reads stems unavailable', async ({
	page
}) => {
	test.setTimeout(120_000);
	const stemRequests: string[] = [];
	page.on('request', (request) => {
		const path = new URL(request.url()).pathname;
		if (/^\/api\/v1\/tracks\/[^/]+\/stems(\/|$)/.test(path)) stemRequests.push(path);
	});

	await page.goto('/music-player?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(
		() => window.musicDjToolsTrackify?.version === 1 && window.musicDjToolsPerformance?.version === 1
	);
	const trackId = await page.evaluate(async () => {
		const response = await fetch('/api/v1/tracks?limit=1');
		if (!response.ok) throw new Error(`tracks list failed (${response.status})`);
		const payload = await response.json();
		const items = Array.isArray(payload.items) ? payload.items : [];
		if (items.length === 0) throw new Error('fixture library has no tracks');
		return items[0].stable_id as string;
	});
	await page.evaluate(async (sid) => {
		const ipc = window.musicDjToolsTrackify!;
		if (ipc.e2e_prime_feed === undefined || ipc.e2e_force_load === undefined) {
			throw new Error('Trackify e2e hooks are unavailable');
		}
		ipc.toggle_autoplay(false, true);
		ipc.e2e_prime_feed([
			{ stable_id: sid, key: '8A', bpm: 124, file_exists: true, title: 'Good', artist: 'E2E' }
		]);
		await ipc.e2e_force_load(sid);
	}, trackId);
	await page.waitForFunction(
		(sid) => window.musicDjToolsTrackify?.query().deck.stable_id === sid,
		trackId,
		{ timeout: 15_000 }
	);
	const stems = await page.evaluate(() => window.musicDjToolsPerformance!.query().decks[1].stems);
	expect(stems.status).toBe('unavailable');
	expect(stems.error ?? '').toMatch(/stems disabled: Trackify/);
	// A Gig load probes stems right after the swap; give a leaked Trackify probe the same chance.
	await page.waitForTimeout(3_000);
	expect(stemRequests).toEqual([]);

	// Positive control: the counter above does see a stem request when one is made.
	const status = await page.evaluate(
		async (sid) => (await fetch(`/api/v1/tracks/${sid}/stems`)).status,
		trackId
	);
	expect(status).toBeGreaterThan(0);
	expect(stemRequests).toEqual([`/api/v1/tracks/${trackId}/stems`]);
});
