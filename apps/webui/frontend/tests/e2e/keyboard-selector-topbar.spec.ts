/**
 * #13700 (epic #9372): the /performance topbar controls are keyboard selector leaves
 * under `topbar`. Drives the live tree through window.__odjSelector, as
 * keyboard-selector-tree.spec.ts does.
 *
 * Acceptance:
 * - [if] the topbar section is selected and E is pressed [then] the selection is its first button in reading order [else stop]
 * - [if] a topbar leaf is selected and D is pressed [then] the selection moves to the next topbar button to the right [else stop]
 * - [if] a topbar button is hidden by layout [then] it is not a live node [else stop]
 *
 * deck_layout persists to the shared ui-prefs.json, so afterEach puts it back.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import { waitForPerformanceIpc } from './support/performance-ready';
import { watchUiPrefsPuts, type UiPrefsPutWatch } from './support/ui-prefs-puts';

type TopbarLeaf = { id: string; x: number };

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

async function boot(page: Page, layout: 'MORE' | 'LESS'): Promise<void> {
	await page.setViewportSize({ width: 1440, height: 1000 });
	await page.goto('/performance');
	await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible({ timeout: 30_000 });
	await waitForPerformanceIpc(page);
	await page
		.locator('.deck-layout-toggle')
		.getByRole('button', { name: layout, exact: true })
		.click();
	if (layout === 'LESS') await expect(page.locator('.rb-topbar')).toHaveClass(/less-view/);
	else await expect(page.locator('.rb-topbar')).not.toHaveClass(/less-view/);
	// Let the deck LESS/MORE transition and the ResizeObserver re-registration settle.
	await page.waitForTimeout(400);
}

/** Live topbar leaves sorted left to right by their measured x. */
const topbarLeaves = (page: Page): Promise<TopbarLeaf[]> =>
	page.evaluate(() =>
		window
			.__odjSelector!.nodes()
			.filter((n) => n.parentId === 'topbar')
			.map((n) => ({ id: n.id, x: n.rect.x }))
			.sort((a, b) => a.x - b.x)
	);
const start = (page: Page, id: string): Promise<string> =>
	page.evaluate((i) => window.__odjSelector!.start(i), id);

test('#13700: drilling into the topbar lands on its leftmost control', async ({ page }) => {
	test.setTimeout(90_000);
	await boot(page, 'MORE');
	const leaves = await topbarLeaves(page);
	expect(leaves.length, 'topbar has selector leaves').toBeGreaterThan(5);
	expect(leaves.every((l) => l.id.startsWith('topbar.'))).toBe(true);
	await start(page, 'topbar');
	const first = await page.evaluate(() => window.__odjSelector!.drillIn());
	expect(first).toBe(leaves[0].id);
	expect(first).toBe('topbar.analysis-source');
});

test('#13700: moving right from a topbar leaf reaches the next control to its right', async ({
	page
}) => {
	test.setTimeout(90_000);
	await boot(page, 'MORE');
	const leaves = await topbarLeaves(page);
	await start(page, leaves[0].id);
	let previousX = leaves[0].x;
	for (let i = 0; i < 6; i++) {
		const id = await page.evaluate(() => window.__odjSelector!.move('right'));
		const leaf = leaves.find((l) => l.id === id);
		expect(leaf, `move right left the topbar: ${id}`).toBeDefined();
		expect(leaf!.x, `${id} is right of the previous control`).toBeGreaterThan(previousX);
		previousX = leaf!.x;
	}
	await start(page, 'topbar.settings');
	const fromSettings = await page.evaluate(() => window.__odjSelector!.move('right'));
	const settingsX = leaves.find((l) => l.id === 'topbar.settings')!.x;
	const nextRight = leaves.find((l) => l.x > settingsX)!;
	expect(fromSettings).toBe(nextRight.id);
});

test('#13700: a topbar control hidden by LESS view is not a live node', async ({ page }) => {
	test.setTimeout(90_000);
	await boot(page, 'MORE');
	expect((await topbarLeaves(page)).map((l) => l.id)).toContain('topbar.command');
	await boot(page, 'LESS');
	await expect(page.locator('.rb-topbar .cmd-entry')).toBeHidden();
	const ids = (await topbarLeaves(page)).map((l) => l.id);
	expect(ids, 'LESS hides the command entry').not.toContain('topbar.command');
	expect(ids).toContain('topbar.settings');
});
