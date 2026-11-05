/**
 * requirement: TOPVIEW-01
 *
 * List view gives the waveform row's height to the library and nothing else
 * (Sol P1 4287872592 on #6824). Measured from the real /performance grid's
 * resolved track sizes, in MORE and LESS, at a short and a tall window.
 *
 * Regression lines:
 * - if switching to List view changes the deck/mixer track height then broken
 * - if the library row does not gain exactly the waveform row's height then broken
 *
 * Both perf_view and deck_layout persist to the shared root engine's
 * ui-prefs.json, and every later root spec hydrates from it. On a fast runner
 * the page closed with its last "waveform" PUT still queued behind "list", so
 * the next specs (top bar width sweep, border audit) opened /performance in
 * List view and failed (#6824, e2e run 38904162032). afterEach settles the
 * page's PUTs, then puts the disk back to what this spec found and reads it back.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import { watchUiPrefsPuts, type UiPrefsPutWatch } from './support/ui-prefs-puts';

const RESTORED_KEYS = ['perf_view', 'deck_layout'] as const;

async function diskPrefs(request: APIRequestContext): Promise<Record<string, unknown>> {
	const res = await request.get('/api/v1/ui-prefs');
	expect(res.ok(), await res.text()).toBeTruthy();
	return (await res.json()) as Record<string, unknown>;
}

/** The disk values this spec changes, as they were before it ran. */
let before: Record<string, unknown> | null = null;
let activeWatch: UiPrefsPutWatch | null = null;

test.beforeEach(async ({ page, request }) => {
	const disk = await diskPrefs(request);
	before = {};
	for (const key of RESTORED_KEYS) if (key in disk) before[key] = disk[key];
	expect(before.perf_view, 'the disk names a perf_view to restore').toBeDefined();
	activeWatch = watchUiPrefsPuts(page);
});

test.afterEach(async ({ request }) => {
	const watch = activeWatch;
	activeWatch = null;
	const restore = before;
	before = null;
	try {
		await watch?.settle();
	} finally {
		// Restore even when settle() fails: its failure is this test's to report,
		// and the specs after it must still start from the disk this one found.
		if (restore !== null) {
			const res = await request.put('/api/v1/ui-prefs', { data: restore });
			expect(res.ok(), await res.text()).toBeTruthy();
			const disk = await diskPrefs(request);
			for (const [key, value] of Object.entries(restore)) {
				expect(disk[key], `ui-prefs ${key} is back to what this spec found`).toEqual(value);
			}
		}
	}
});

type Tracks = { topbar: number; wave: number; decks: number; library: number };

async function tracks(page: Page): Promise<Tracks> {
	const raw = await page
		.locator('.perf-root')
		.evaluate((el) => getComputedStyle(el).gridTemplateRows);
	const px = raw.split(' ').map((v) => Number.parseFloat(v));
	expect(px.length, `four resolved tracks, got "${raw}"`).toBe(4);
	for (const v of px) expect(Number.isFinite(v), `"${raw}" resolves to pixels`).toBe(true);
	const [topbar, wave, decks, library] = px;
	return { topbar, wave, decks, library };
}

async function setView(page: Page, view: 'waveform' | 'list'): Promise<void> {
	await page.evaluate(async (v) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		await ipc.dispatch({ type: 'set_perf_view', view: v });
	}, view);
	if (view === 'list') await expect(page.locator('.perf-root')).toHaveClass(/perf-view-list/);
	else await expect(page.locator('.perf-root')).not.toHaveClass(/perf-view-list/);
}

async function setLayout(page: Page, mode: 'MORE' | 'LESS'): Promise<void> {
	await page
		.locator('.deck-layout-toggle')
		.getByRole('button', { name: mode, exact: true })
		.click();
	if (mode === 'LESS') await expect(page.locator('.perf-root')).toHaveClass(/deck-layout-less/);
	else await expect(page.locator('.perf-root')).not.toHaveClass(/deck-layout-less/);
}

// 970 sits inside the band the old rule broke: with four 43px waveform rows
// the deck track resolves to 970 - 28 - 172 - 272 = 498px, between its 497px
// floor and 500px ceiling, so zeroing the rows instead let it grow to 500.
// 720 and 1080 pin the floor and the ceiling.
for (const height of [720, 970, 1080]) {
	for (const layout of ['MORE', 'LESS'] as const) {
		test(`TOPVIEW-01: List view changes only the waveform and library rows (${layout}, 1280x${height})`, async ({
			page
		}) => {
			test.setTimeout(90_000);
			await page.setViewportSize({ width: 1280, height });
			await page.goto('/performance');
			await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
			await setLayout(page, layout);
			await setView(page, 'waveform');
			const wave = await tracks(page);
			expect(wave.wave, 'the waveform row has height in waveform view').toBeGreaterThan(0);

			await setView(page, 'list');
			const list = await tracks(page);
			test.info().annotations.push({ type: 'tracks', description: JSON.stringify({ wave, list }) });

			expect(list.wave, 'List view collapses the waveform row').toBe(0);
			expect(list.topbar).toBe(wave.topbar);
			expect(
				list.decks,
				'if switching to List view changes the deck/mixer track height then broken'
			).toBeCloseTo(wave.decks, 1);
			expect(list.library, 'the library row gains exactly the waveform row height').toBeCloseTo(
				wave.library + wave.wave,
				1
			);

			await setView(page, 'waveform');
			expect(await tracks(page), 'switching back restores every track').toEqual(wave);
		});
	}
}
