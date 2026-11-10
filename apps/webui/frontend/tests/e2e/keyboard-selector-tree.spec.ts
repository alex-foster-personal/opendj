/**
 * requirement: A11Y-9723 (#9397, K5 of epic #9372)
 *
 * /performance registers its sections, deck parts and mixer controls as keyboard
 * selector nodes. K3 (the WASD binding) is not on main yet, so this drives the live
 * tree through window.__odjSelector, the same handle agents use.
 *
 * Acceptance:
 * - [if] four-deck mode renders [then] the root sections are topbar, waveforms, decks, library in reading order [then stop]
 * - [if] decks is drilled into once, then twice [then] the selection is deck.1, then deck.1.header [then stop]
 * - [if] channel 4 LOW is reached by right/down moves from the mixer [then] its id is mixer.ch4.low [then stop]
 * - [if] two-deck mode hides decks 3 and 4 [then] their nodes leave the tree and no move lands on them [then stop]
 *
 * deck_layout persists to the shared ui-prefs.json, so afterEach puts it back the way
 * topbar-list-view-geometry.spec.ts does.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import { waitForPerformanceIpc } from './support/performance-ready';
import { watchUiPrefsPuts, type UiPrefsPutWatch } from './support/ui-prefs-puts';

type Dir = 'up' | 'down' | 'left' | 'right';

async function diskPrefs(request: APIRequestContext): Promise<Record<string, unknown>> {
	const res = await request.get('/api/v1/ui-prefs');
	expect(res.ok(), await res.text()).toBeTruthy();
	return (await res.json()) as Record<string, unknown>;
}

let before: Record<string, unknown> | null = null;
let activeWatch: UiPrefsPutWatch | null = null;

test.beforeEach(async ({ page, request }) => {
	const disk = await diskPrefs(request);
	// A fresh data dir has no deck_layout on disk: nothing to restore then.
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
			expect((await diskPrefs(request)).deck_layout).toEqual(restore.deck_layout);
		}
	}
});

async function boot(page: Page, layout: 'MORE' | 'LESS'): Promise<void> {
	await page.setViewportSize({ width: 1440, height: 1000 });
	await page.goto('/performance');
	await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible({ timeout: 30_000 });
	await waitForPerformanceIpc(page);
	await setLayout(page, layout);
}

async function setLayout(page: Page, mode: 'MORE' | 'LESS'): Promise<void> {
	await page
		.locator('.deck-layout-toggle')
		.getByRole('button', { name: mode, exact: true })
		.click();
	if (mode === 'LESS') await expect(page.locator('.perf-root')).toHaveClass(/deck-layout-less/);
	else await expect(page.locator('.perf-root')).not.toHaveClass(/deck-layout-less/);
	// Let the 200ms deck LESS/MORE transition settle before rects are measured.
	await page.waitForTimeout(400);
}

const nodeIds = (page: Page): Promise<string[]> =>
	page.evaluate(() => window.__odjSelector!.nodes().map((n) => n.id));
const rootIds = (page: Page): Promise<string[]> =>
	page.evaluate(() =>
		window
			.__odjSelector!.nodes()
			.filter((n) => n.parentId === null)
			.map((n) => n.id)
	);
const start = (page: Page, id: string): Promise<string> =>
	page.evaluate((i) => window.__odjSelector!.start(i), id);
const drillIn = (page: Page): Promise<string> =>
	page.evaluate(() => window.__odjSelector!.drillIn());
const move = (page: Page, dir: Dir): Promise<string> =>
	page.evaluate((d) => window.__odjSelector!.move(d), dir);

async function moveUntil(
	page: Page,
	dir: Dir,
	target: string,
	maxSteps: number
): Promise<string[]> {
	const path: string[] = [];
	for (let i = 0; i < maxSteps; i++) {
		const id = await move(page, dir);
		path.push(id);
		if (id === target) return path;
	}
	throw new Error(`moving ${dir} never reached ${target}: ${path.join(' -> ')}`);
}

test('A11Y-9723: four-deck roots, drill into deck.1.header, reach mixer.ch4.low', async ({
	page
}) => {
	test.setTimeout(90_000);
	await boot(page, 'MORE');

	expect(await rootIds(page)).toEqual(['topbar', 'waveforms', 'decks', 'library']);
	const ids = await nodeIds(page);
	for (const n of [1, 2, 3, 4]) expect(ids).toContain(`deck.${n}.header`);

	expect(await start(page, 'decks')).toBe('decks');
	expect(await drillIn(page)).toBe('deck.1');
	expect(await drillIn(page)).toBe('deck.1.header');

	await start(page, 'deck.1');
	await moveUntil(page, 'right', 'mixer', 3);
	const landed = await drillIn(page);
	expect(landed.startsWith('mixer.'), `mixer drill-in lands on a mixer child, got ${landed}`).toBe(
		true
	);
	if (landed !== 'mixer.ch4') await moveUntil(page, 'right', 'mixer.ch4', 4);
	await drillIn(page);
	await moveUntil(page, 'down', 'mixer.ch4.low', 6);
	await expect(page.locator('[data-sel-node="mixer.ch4.low"]')).toHaveCount(1);
});

test('A11Y-9723: two-deck mode drops decks 3 and 4 from the selector', async ({ page }) => {
	test.setTimeout(90_000);
	await boot(page, 'LESS');

	const ids = await nodeIds(page);
	const hidden = ids.filter((id) => /^(deck\.[34]|mixer\.ch[34])(\.|$)/.test(id));
	expect(hidden, 'no deck 3/4 or channel 3/4 nodes in LESS').toEqual([]);
	await expect(page.locator('.rb-deck[data-deck="3"]')).toHaveAttribute('data-sel-disabled', '');
	await expect(page.locator('.rb-deck[data-deck="4"]')).toHaveAttribute('data-sel-disabled', '');

	await start(page, 'deck.1');
	const dirs: Dir[] = [
		'down',
		'right',
		'down',
		'right',
		'left',
		'down',
		'up',
		'right',
		'down',
		'left',
		'left',
		'down'
	];
	for (const dir of dirs) {
		const id = await move(page, dir);
		expect(id, `move ${dir} stays off hidden decks`).not.toMatch(/^deck\.[34]$/);
	}

	await setLayout(page, 'MORE');
	expect(await nodeIds(page)).toContain('deck.3');
});
