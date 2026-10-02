// requirement: LIBM-134
/**
 * LIBM-134 against the real engine and the production build: opening the
 * 1,000-member "Perf 1k" playlist requests a 30-row first page and then
 * 500-row pages, and the pane ends up holding every member once, in the
 * membership order `GET /api/v1/playlists/{id}` reports.
 *
 * Runs under playwright.playlist-switch-latency.config.ts (same fixture).
 */
import { expect, test, type Page } from '@playwright/test';

const PLAYLIST_ID = 'pl-perf-1k';
const PLAYLIST_NAME = 'Perf 1k';

interface PerfRingRow {
	kind: string;
	stages?: Record<string, number>;
}

interface PerfRingWindow extends Window {
	__mdtPerfLog?: () => readonly PerfRingRow[];
}

interface TracksPageCall {
	offset: number;
	limit: number;
	status: number;
	stableIds: string[];
}

function recordTracksPages(page: Page): TracksPageCall[] {
	const calls: TracksPageCall[] = [];
	page.on('response', async (response) => {
		const url = new URL(response.url());
		if (url.pathname !== `/api/v1/playlists/${PLAYLIST_ID}/tracks`) return;
		const ok = response.status() === 200;
		const body = ok ? ((await response.json()) as { tracks: { stable_id: string }[] }) : null;
		calls.push({
			offset: Number(url.searchParams.get('offset')),
			limit: Number(url.searchParams.get('limit')),
			status: response.status(),
			stableIds: body?.tracks.map((t) => t.stable_id) ?? []
		});
	});
	return calls;
}

async function waitForPlaylistFill(page: Page, before: number): Promise<number> {
	const handle = await page.waitForFunction(
		(start) => {
			const rows = (window as PerfRingWindow).__mdtPerfLog?.() ?? [];
			const done = rows.slice(start).find((row) => row.kind === 'library-load-playlist');
			return done?.stages?.rows ?? null;
		},
		before,
		{ timeout: 60_000 }
	);
	return (await handle.jsonValue()) as number;
}

// REQ: LIBM-134
test('a 1k playlist fills with a 30-row first page then 500-row pages, every member once in order', async ({
	page,
	request
}) => {
	const detail = await request.get(`/api/v1/playlists/${PLAYLIST_ID}`);
	expect(detail.ok()).toBeTruthy();
	const membership = ((await detail.json()) as { items: string[] }).items;
	expect(membership.length).toBe(1000);

	// Record from before boot: PERF-UI-05's tree-intent prefetch (#3746) may
	// request page 1 before the click, and the switch then joins that GET.
	const calls = recordTracksPages(page);
	await page.addInitScript(() => localStorage.removeItem('odj.brand-launch.v1'));
	await page.goto('/performance', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(
		() => typeof (window as PerfRingWindow).__mdtPerfLog === 'function',
		undefined,
		{ timeout: 60_000 }
	);
	const playlistRow = page.getByTestId('playlist-row').filter({ hasText: PLAYLIST_NAME });
	await expect(playlistRow).toBeVisible({ timeout: 60_000 });

	const before = await page.evaluate(() => (window as PerfRingWindow).__mdtPerfLog?.().length ?? 0);
	await playlistRow.click();
	const filledRows = await waitForPlaylistFill(page, before);
	await expect.poll(() => calls.filter((c) => c.offset > 0).length).toBe(2);

	// A prefetch older than its max age is replaced by a fresh one, so page 1
	// can be requested more than once; the pane paints the latest.
	const firstPages = calls.filter((c) => c.offset === 0);
	expect(firstPages.length).toBeGreaterThanOrEqual(1);
	const pages = [firstPages[firstPages.length - 1], ...calls.filter((c) => c.offset > 0)];
	pages.sort((a, b) => a.offset - b.offset);
	expect(pages.map((c) => c.status)).toEqual([200, 200, 200]);
	expect(pages.map((c) => [c.offset, c.limit])).toEqual([
		[0, 30],
		[30, 500],
		[530, 500]
	]);
	expect(filledRows).toBe(membership.length);
	expect(pages.flatMap((c) => c.stableIds)).toEqual(membership);
});
