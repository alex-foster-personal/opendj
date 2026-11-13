/**
 * #13696 (epic #9372): the channel strip's cue button, colour FX slot and R/M
 * calibration buttons are keyboard selector nodes under `mixer.ch<n>`. Drives the live
 * tree through window.__odjSelector, as keyboard-selector-tree.spec.ts does.
 *
 * Acceptance:
 * - [if] the mixer channel 1 group is drilled into [then] its children include mixer.ch1.cue, mixer.ch1.fx and mixer.ch1.cal [else stop]
 * - [if] the selector moves from the mixer.ch1.fader region [then] it reaches the cue button by moves alone [else stop]
 * - [if] the channel is disabled (deck 3/4 in two-deck mode) [then] its new leaves are dropped with it [else stop]
 *
 * deck_layout persists to the shared ui-prefs.json, so afterEach puts it back.
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
	if (layout === 'LESS') await expect(page.locator('.perf-root')).toHaveClass(/deck-layout-less/);
	else await expect(page.locator('.perf-root')).not.toHaveClass(/deck-layout-less/);
	// Let the 200ms deck LESS/MORE transition settle before rects are measured.
	await page.waitForTimeout(400);
}

const childIds = (page: Page, parentId: string): Promise<string[]> =>
	page.evaluate(
		(p) =>
			window
				.__odjSelector!.nodes()
				.filter((n) => n.parentId === p)
				.map((n) => n.id),
		parentId
	);
const nodeIds = (page: Page): Promise<string[]> =>
	page.evaluate(() => window.__odjSelector!.nodes().map((n) => n.id));
const start = (page: Page, id: string): Promise<string> =>
	page.evaluate((i) => window.__odjSelector!.start(i), id);
const move = (page: Page, dir: Dir): Promise<string> =>
	page.evaluate((d) => window.__odjSelector!.move(d), dir);

async function moveUntil(page: Page, dir: Dir, target: string, maxSteps: number): Promise<void> {
	const path: string[] = [];
	for (let i = 0; i < maxSteps; i++) {
		const id = await move(page, dir);
		path.push(id);
		if (id === target) return;
	}
	throw new Error(`moving ${dir} never reached ${target}: ${path.join(' -> ')}`);
}

test('#13696: channel 1 children include cue, fx and cal', async ({ page }) => {
	test.setTimeout(90_000);
	await boot(page, 'MORE');
	const kids = await childIds(page, 'mixer.ch1');
	for (const id of ['mixer.ch1.cue', 'mixer.ch1.fx', 'mixer.ch1.cal']) expect(kids).toContain(id);
	expect((await childIds(page, 'mixer.ch1.cal')).sort()).toEqual([
		'mixer.ch1.cal.ceiling',
		'mixer.ch1.cal.red'
	]);
	expect(await childIds(page, 'mixer.ch1.fx')).toContain('mixer.ch1.filter');
	await expect(page.locator('[data-sel-node="mixer.ch1.cue"]')).toHaveAttribute(
		'data-testid',
		'cue-channel-1'
	);
});

test('#13696: the cue button is reachable from the fader region by moves alone', async ({
	page
}) => {
	test.setTimeout(90_000);
	await boot(page, 'MORE');
	await start(page, 'mixer.ch1.low');
	await moveUntil(page, 'down', 'mixer.ch1.cue', 3);
	await start(page, 'mixer.ch1.fader');
	await moveUntil(page, 'up', 'mixer.ch1.cue', 1);
});

test('#13696: two-deck mode drops channel 3/4 buttons and hides R|M', async ({ page }) => {
	test.setTimeout(90_000);
	await boot(page, 'LESS');
	const ids = await nodeIds(page);
	const leaked = ids.filter((id) => /^mixer\.ch[34]\.(cue|fx|cal)(\.|$)/.test(id));
	expect(leaked, 'no channel 3/4 button nodes in LESS').toEqual([]);
	expect(ids).toContain('mixer.ch1.cue');
	expect(ids).toContain('mixer.ch1.fx');
	expect(
		ids.filter((id) => id.startsWith('mixer.ch1.cal')),
		'R|M is hidden in LESS'
	).toEqual([]);
});
