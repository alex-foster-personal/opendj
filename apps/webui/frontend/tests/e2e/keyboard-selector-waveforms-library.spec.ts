/**
 * #13703 (epic #9372): each deck's waveform lane and the library's tree, search box and
 * track table are keyboard selector leaves. Drives the live tree through
 * window.__odjSelector, as keyboard-selector-tree.spec.ts does.
 *
 * Acceptance:
 * - [if] the waveforms section is drilled into [then] each visible deck lane is a leaf in top to bottom order [else stop]
 * - [if] the waveforms section is listed [then] the edge sampler chip (#13722) is its one other leaf, `waveforms.sampler` [else stop]
 * - [if] the library section is drilled into [then] tree, search and tracks are leaves [else stop]
 * - [if] two-deck mode hides lanes 3 and 4 [then] those leaves are not live [else stop]
 *
 * deck_layout persists to the shared ui-prefs.json, so afterEach puts it back the way
 * keyboard-selector-tree.spec.ts does.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import { waitForPerformanceIpc } from './support/performance-ready';
import { watchUiPrefsPuts, type UiPrefsPutWatch } from './support/ui-prefs-puts';

type SelNode = { id: string; parentId: string | null; kind: string };

async function diskPrefs(request: APIRequestContext): Promise<Record<string, unknown>> {
	const res = await request.get('/api/v1/ui-prefs');
	expect(res.ok(), await res.text()).toBeTruthy();
	return (await res.json()) as Record<string, unknown>;
}

let before: Record<string, unknown> | null = null;
let activeWatch: UiPrefsPutWatch | null = null;

test.beforeEach(async ({ page, request }) => {
	const disk = await diskPrefs(request);
	before = 'deck_layout' in disk ? { deck_layout: disk.deck_layout } : null;
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
		if (restore !== null) {
			const res = await request.put('/api/v1/ui-prefs', { data: restore });
			expect(res.ok(), await res.text()).toBeTruthy();
		}
	}
});

async function boot(page: Page, mode: 'MORE' | 'LESS'): Promise<void> {
	await page.setViewportSize({ width: 1440, height: 1000 });
	await page.goto('/performance');
	await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible({ timeout: 30_000 });
	await waitForPerformanceIpc(page);
	await page
		.locator('.deck-layout-toggle')
		.getByRole('button', { name: mode, exact: true })
		.click();
	if (mode === 'LESS') await expect(page.locator('.perf-root')).toHaveClass(/deck-layout-less/);
	else await expect(page.locator('.perf-root')).not.toHaveClass(/deck-layout-less/);
	// Let the 200ms deck LESS/MORE transition settle before rects are measured.
	await page.waitForTimeout(400);
}

const nodes = (page: Page): Promise<SelNode[]> =>
	page.evaluate(() =>
		window.__odjSelector!.nodes().map((n) => ({ id: n.id, parentId: n.parentId, kind: n.kind }))
	);
const childIds = async (page: Page, parentId: string): Promise<string[]> =>
	(await nodes(page)).filter((n) => n.parentId === parentId).map((n) => n.id);
const kindOf = async (page: Page, id: string): Promise<string | undefined> =>
	(await nodes(page)).find((n) => n.id === id)?.kind;
const start = (page: Page, id: string): Promise<string> =>
	page.evaluate((i) => window.__odjSelector!.start(i), id);
const drillIn = (page: Page): Promise<string> =>
	page.evaluate(() => window.__odjSelector!.drillIn());
const move = (page: Page, dir: 'up' | 'down' | 'left' | 'right'): Promise<string> =>
	page.evaluate((d) => window.__odjSelector!.move(d), dir);
/** The waveform stack's edge sampler on/off chip (#13722) is a leaf beside the lanes. */
const SAMPLER = 'waveforms.sampler';

test('#13703: the waveforms section lists one leaf per lane, top to bottom', async ({ page }) => {
	test.setTimeout(90_000);
	await boot(page, 'MORE');
	const lanes = [1, 2, 3, 4].map((d) => `waveforms.deck${d}`);
	expect((await childIds(page, 'waveforms')).sort()).toEqual([...lanes, SAMPLER]);
	for (const id of [...lanes, SAMPLER]) expect(await kindOf(page, id)).toBe('leaf');
	await start(page, 'waveforms');
	expect(await drillIn(page)).toBe('waveforms.deck1');
	for (const id of lanes.slice(1)) expect(await move(page, 'down')).toBe(id);
});

test('#13703: the library section lists tree, search and tracks leaves', async ({ page }) => {
	test.setTimeout(90_000);
	await boot(page, 'MORE');
	const panes = ['library.search', 'library.tracks', 'library.tree'];
	expect((await childIds(page, 'library')).sort()).toEqual(panes);
	for (const id of panes) expect(await kindOf(page, id)).toBe('leaf');
	await start(page, 'library');
	expect(panes).toContain(await drillIn(page));
});

test('#13703: two-deck mode drops lanes 3 and 4', async ({ page }) => {
	test.setTimeout(90_000);
	await boot(page, 'LESS');
	expect((await childIds(page, 'waveforms')).sort()).toEqual([
		'waveforms.deck1',
		'waveforms.deck2',
		SAMPLER
	]);
	await start(page, 'waveforms.deck2');
	expect(await move(page, 'down')).toBe('waveforms.deck2');
});
