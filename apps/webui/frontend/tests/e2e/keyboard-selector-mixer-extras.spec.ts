/**
 * #13706 (epic #9372): the crossfader assign switches, the crossfade curve select, the
 * group pad and the edge sampler are keyboard selector nodes. Drives the live tree through
 * window.__odjSelector, as keyboard-selector-tree.spec.ts does.
 *
 * Acceptance:
 * - [if] the mixer group is drilled into [then] the assign switches, curve select and group pad are leaves [else stop]
 * - [if] the sampler is visible [then] it is a reachable selector group whose pads are leaves [else stop]
 * - [if] a control is hidden [then] its node is not live [else stop]
 *
 * The edge sampler mounts only in the Electron shell, so its test skips in a plain browser.
 */
import { expect, test, type Page } from '@playwright/test';

import { waitForPerformanceIpc } from './support/performance-ready';

type SelNode = { id: string; parentId: string | null; kind: string };

async function boot(page: Page): Promise<void> {
	await page.setViewportSize({ width: 1440, height: 1000 });
	await page.goto('/performance');
	await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible({ timeout: 30_000 });
	await waitForPerformanceIpc(page);
}

const nodes = (page: Page): Promise<SelNode[]> =>
	page.evaluate(() =>
		window.__odjSelector!.nodes().map((n) => ({ id: n.id, parentId: n.parentId, kind: n.kind }))
	);
const childIds = async (page: Page, parentId: string): Promise<string[]> =>
	(await nodes(page)).filter((n) => n.parentId === parentId).map((n) => n.id);
const kindOf = async (page: Page, id: string): Promise<string | undefined> =>
	(await nodes(page)).find((n) => n.id === id)?.kind;

async function openGroupPad(page: Page): Promise<void> {
	await page.locator('[data-knob-id="1:high"]').click({ modifiers: ['Shift'] });
	await page.locator('[data-knob-id="1:mid"]').click({ modifiers: ['Shift'] });
	await expect(page.locator('[data-testid="mixer-group-pad"]')).toBeVisible();
}

test('#13706: the mixer lists the assign switches, curve select and group pad', async ({
	page
}) => {
	test.setTimeout(90_000);
	await boot(page);
	await openGroupPad(page);
	const kids = await childIds(page, 'mixer');
	for (const id of [
		'mixer.assign.a',
		'mixer.assign.b',
		'mixer.crossfade-curve',
		'mixer.group-pad'
	]) {
		expect(kids).toContain(id);
	}
	expect(await kindOf(page, 'mixer.crossfade-curve')).toBe('leaf');
	expect(await kindOf(page, 'mixer.group-pad')).toBe('leaf');
	for (const bus of ['a', 'b']) {
		expect((await childIds(page, `mixer.assign.${bus}`)).sort()).toEqual(
			[1, 2, 3, 4].map((d) => `mixer.assign.${bus}.ch${d}`)
		);
	}
});

test('#13706: a visible edge sampler is a reachable group of pad leaves', async ({ page }) => {
	test.setTimeout(90_000);
	await boot(page);
	test.skip(
		(await page.locator('[data-testid="edge-sampler"]').count()) === 0,
		'edge sampler mounts only in the Electron shell'
	);
	await page.evaluate(() => window.__odjSelector!.start('decks'));
	expect(await page.evaluate(() => window.__odjSelector!.move('left'))).toBe('sampler.l');
	expect(await page.evaluate(() => window.__odjSelector!.drillIn())).toBe('sampler.l.pad1');
	const pads = await childIds(page, 'sampler.l');
	expect(pads.length).toBeGreaterThan(0);
	for (const id of pads) expect(await kindOf(page, id)).toBe('leaf');
});

test('#13706: a closed group pad leaves no live node', async ({ page }) => {
	test.setTimeout(90_000);
	await boot(page);
	await openGroupPad(page);
	expect(await childIds(page, 'mixer')).toContain('mixer.group-pad');
	await page.locator('[data-testid="mixer-group-pad-close"]').click();
	await expect(page.locator('[data-testid="mixer-group-pad"]')).toHaveCount(0);
	expect((await nodes(page)).map((n) => n.id)).not.toContain('mixer.group-pad');
});
