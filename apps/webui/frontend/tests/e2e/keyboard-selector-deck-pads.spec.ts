/**
 * requirement: KEYB-13865 (#13698, epic #9372)
 *
 * Deck pads, pad mode menu, beat jump and grid edit are selector nodes.
 *
 * Drives the live tree through window.__odjSelector, the same handle agents use. Beat jump
 * buttons are disabled with no track loaded, so they register `enabled: false` and leave the
 * resolved tree; their DOM node, kind and parent are checked instead.
 *
 * Acceptance:
 * - [if] deck 1 `cues` is drilled into [then] each of the 8 pads is a leaf `deck.1.cues.pad<k>` [else stop]
 * - [if] deck 1 `loop` is drilled into [then] the beat jump back and forward buttons are leaves [else stop]
 * - [if] the pad mode menu is shown [then] it is a leaf under `deck.<n>.cues` [else stop]
 */
import { expect, test, type Page } from '@playwright/test';

import { waitForPerformanceIpc } from './support/performance-ready';

type NodeView = { id: string; parentId: string | null; kind: string };

async function boot(page: Page): Promise<void> {
	await page.setViewportSize({ width: 1440, height: 1000 });
	await page.goto('/performance');
	await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible({ timeout: 30_000 });
	await waitForPerformanceIpc(page);
	await page.waitForTimeout(400);
}

const liveNodes = (page: Page): Promise<NodeView[]> =>
	page.evaluate(() =>
		window.__odjSelector!.nodes().map((n) => ({ id: n.id, parentId: n.parentId, kind: n.kind }))
	);

/** Kind and nearest registered ancestor of a DOM selector node, whether or not it is enabled. */
const domNode = (
	page: Page,
	id: string
): Promise<{ kind: string | null; parentId: string | null }> =>
	page.evaluate((nodeId) => {
		const el = document.querySelector(`[data-sel-node="${CSS.escape(nodeId)}"]`);
		if (el === null) throw new Error(`no DOM selector node ${nodeId}`);
		const parent = el.parentElement?.closest('[data-sel-node]') ?? null;
		return {
			kind: el.getAttribute('data-sel-kind'),
			parentId: parent?.getAttribute('data-sel-node') ?? null
		};
	}, id);

test('KEYB-13865 deck 1 cues drills into eight pad leaves', async ({ page }) => {
	test.setTimeout(90_000);
	await boot(page);
	const nodes = await liveNodes(page);
	const cues = nodes.find((n) => n.id === 'deck.1.cues');
	expect(cues?.kind).toBe('group');
	const pads = nodes.filter((n) => n.parentId === 'deck.1.cues' && /\.pad\d$/.test(n.id));
	expect(pads.map((n) => n.id).sort()).toEqual(
		[1, 2, 3, 4, 5, 6, 7, 8].map((k) => `deck.1.cues.pad${k}`)
	);
	for (const pad of pads) expect(pad.kind).toBe('leaf');

	await page.evaluate(() => window.__odjSelector!.start('deck.1.cues'));
	const landed = await page.evaluate(() => window.__odjSelector!.drillIn());
	expect(landed.startsWith('deck.1.cues.'), `drill-in lands on a cues child, got ${landed}`).toBe(
		true
	);
});

test('KEYB-13865 deck 1 loop holds beat jump back and forward leaves', async ({ page }) => {
	test.setTimeout(90_000);
	await boot(page);
	const loop = (await liveNodes(page)).find((n) => n.id === 'deck.1.loop');
	expect(loop?.kind).toBe('group');
	for (const dir of ['back', 'forward']) {
		expect(await domNode(page, `deck.1.loop.jump-${dir}`)).toEqual({
			kind: 'leaf',
			parentId: 'deck.1.loop'
		});
	}
	await page.evaluate(() => window.__odjSelector!.start('deck.1.loop'));
	expect(await page.evaluate(() => window.__odjSelector!.drillIn())).toMatch(/^deck\.1\.loop\./);
});

test('KEYB-13865 the pad mode menu is a leaf under deck.<n>.cues', async ({ page }) => {
	test.setTimeout(90_000);
	await boot(page);
	await expect(page.getByTestId('hot-cue-menu-deck-1')).toBeVisible();
	const menu = (await liveNodes(page)).find((n) => n.id === 'deck.1.cues.mode');
	expect(menu).toEqual({ id: 'deck.1.cues.mode', parentId: 'deck.1.cues', kind: 'leaf' });
	await expect(page.getByTestId('hot-cue-menu-deck-1')).toHaveAttribute(
		'data-sel-node',
		'deck.1.cues.mode'
	);
});
