import { expect, test, type Page } from '@playwright/test';

/**
 * AGENT-51: right-click a deck waveform, "Transition in around here", and the track API reads the cue back.
 *
 * Runs under playwright.transition-in-cue.config.ts: real engine, real routers, real fixture library, one seeded
 * own beatgrid record (the route snaps to downbeats, and the generated fixture tracks have none). The menu, the
 * click-to-time mapping, the route, the snap and the store are all the production ones.
 *
 * Regression lines:
 * - if the waveform menu has no "Transition in around here" for a loaded deck then broken
 * - if choosing it stores nothing the track API can read then broken
 * - if the stored cue is not a downbeat of the grid /anlz serves then broken
 * - if the stored cue is not near where the click landed then broken
 * - if the menu PUT succeeds and query().decks[1].transition_in_ms stays null then the waveform read model was not refreshed
 */

type Beat = { n: number; t: number };

test.skip(process.platform === 'win32', 'POSIX-only: guardedWebServerCommand launches /bin/sh and .venv/bin/python');

async function trackWithGrid(page: Page): Promise<{ stableId: string; downbeatsMs: number[] }> {
	const listing = await page.request.get('/api/v1/tracks?limit=50');
	expect(listing.ok()).toBe(true);
	const { items } = (await listing.json()) as { items: { stable_id: string }[] };
	for (const { stable_id } of items) {
		const anlz = await page.request.get(`/api/v1/tracks/${stable_id}/anlz?points=100`);
		if (!anlz.ok()) continue;
		const beats = ((await anlz.json()) as { beatgrid: { beats: Beat[] } }).beatgrid.beats;
		const downbeatsMs = beats.filter((beat) => beat.n === 1).map((beat) => Math.round(beat.t * 1000));
		if (downbeatsMs.length >= 2) return { stableId: stable_id, downbeatsMs };
	}
	throw new Error('no fixture track serves a grid on /anlz: the seeding step did not run');
}

test('the waveform menu sets the transition-in cue and the track API reads it back', async ({ page }) => {
	await page.goto('/performance?muted=1');
	await page.waitForFunction(() => window.musicDjToolsPerformance !== undefined, undefined, { timeout: 60_000 });
	const { stableId, downbeatsMs } = await trackWithGrid(page);
	const cueUrl = `/api/v1/tracks/${stableId}/transition-in`;
	await page.request.delete(cueUrl);
	expect(await (await page.request.get(cueUrl)).json()).toEqual({ cue: null });

	await page.evaluate(async (stable_id) => {
		await window.musicDjToolsPerformance!.dispatch({ type: 'load', deck: 1, stable_id } as never);
	}, stableId);
	const durationOf = () => page.evaluate(() => window.musicDjToolsPerformance!.query().decks[1].duration_ms ?? 0);
	await expect.poll(durationOf).toBeGreaterThan(0);
	const durationMs = await durationOf();

	const strip = page.getByTestId('waveform-seek-deck-1');
	await expect(strip).toBeEnabled();
	const box = (await strip.boundingBox())!;
	const fraction = 0.4;
	await strip.click({ button: 'right', position: { x: box.width * fraction, y: box.height / 2 } });
	const item = page.getByTestId('quick-draw-transition-in');
	await expect(item).toHaveText('Transition in around here');
	await expect(item).toBeEnabled();
	await item.click();

	await expect.poll(async () => (await (await page.request.get(cueUrl)).json()).cue).not.toBeNull();
	const { cue } = (await (await page.request.get(cueUrl)).json()) as { cue: { kind: string; in_ms: number } };
	expect(cue.kind).toBe('transition_in');
	expect(downbeatsMs, 'the stored cue must sit on a served downbeat').toContain(cue.in_ms);
	const clickedMs = fraction * durationMs;
	const barMs = Math.max(...downbeatsMs.slice(1).map((t, i) => t - downbeatsMs[i]));
	expect(Math.abs(cue.in_ms - clickedMs), 'snapped within one bar of the click').toBeLessThanOrEqual(barMs);

	await expect
		.poll(async () => page.evaluate(() => window.musicDjToolsPerformance!.query().decks[1].transition_in_ms))
		.toBe(cue.in_ms);
});
