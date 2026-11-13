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
 * - [if] selector movement selects a different node while Tab focus stays put [then] focus-visible and selector rings stay distinguishable [else stop]
 * - [if] the focused channel 1 fader re-mounts in the LESS swap [then] its new slider holds focus (#13428) [then stop]
 * - [if] a deck is drilled into from the decks section [then] drill lands on deck.N then a deck part [then stop]
 * - [if] each declared deck part is present in MORE mode [then] every inventory id is in the tree [then stop]
 * - [if] a deck part is disabled or removed by layout [then] its ids leave the snapshot [then stop]
 *
 * deck_layout persists to the shared ui-prefs.json, so afterEach puts it back the way
 * topbar-list-view-geometry.spec.ts does.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import type { SelectorCommandId } from '../../src/lib/keyboard/command-registry';
import { waitForPerformanceIpc } from './support/performance-ready';
import {
	assertFocusAndSelectorRingsDistinguishable,
	readFocusSelectorCoexistence,
	syncSelectorRingToDomSelection
} from './support/selector-focus-ring';
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

async function loadDeckOneForSelectorFocus(page: Page): Promise<void> {
	const stableId = await page
		.locator('[data-testid="track-row"]')
		.first()
		.getAttribute('data-stable-id');
	expect(stableId, 'fixture row has no data-stable-id').toBeTruthy();
	await page.evaluate(async (id) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		await ipc.dispatch({ type: 'load', deck: 1, stable_id: id });
	}, stableId as string);
	await expect(page.locator('[data-testid="play-deck-1"]')).toBeEnabled({ timeout: 15_000 });
}

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
const deckPartIds = (page: Page, deck: number): Promise<readonly string[]> =>
	page.evaluate((n) => window.__odjSelector!.deckPartNodeIds(n as 1 | 2 | 3 | 4), deck);
const deckParts = (page: Page): Promise<readonly string[]> =>
	page.evaluate(() => [...window.__odjSelector!.deckParts()]);

test('selector commands are discoverable and share the agent handle selection', async ({
	page
}) => {
	test.setTimeout(90_000);
	await boot(page, 'MORE');
	await start(page, 'decks');
	const commands = await page.evaluate(() => window.__odjSelector!.commands());
	expect(commands.map((command) => command.id).sort()).toEqual([
		'selector.drill-in',
		'selector.drill-out',
		'selector.move.down',
		'selector.move.left',
		'selector.move.right',
		'selector.move.up'
	]);
	for (const command of commands) {
		expect(command.label.trim().length).toBeGreaterThan(0);
		expect(command.scope).toBe('global');
		expect(command).not.toHaveProperty('run');
	}
	expect(await page.evaluate(() => window.__odjSelector!.selected())).toBe('decks');
	const invoke = async (id: SelectorCommandId, expected: string): Promise<void> => {
		const result = await page.evaluate((commandId) => {
			const selector = window.__odjSelector!;
			return { returned: selector.run(commandId), selected: selector.selected() };
		}, id);
		expect(result).toEqual({ returned: expected, selected: expected });
	};
	await invoke('selector.drill-out', 'decks');
	await invoke('selector.drill-in', 'deck.1');
	expect(await drillIn(page)).toBe('deck.1.header');
	await invoke('selector.drill-in', 'deck.1.header');
	await invoke('selector.drill-out', 'deck.1');
	await invoke('selector.move.right', 'mixer');
	expect(await move(page, 'left')).toBe('deck.1');
	await expect(
		page.evaluate(() => Reflect.apply(window.__odjSelector!.run, null, ['selector.unknown']))
	).rejects.toThrow(/Unknown selector command/);
});

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

test('[if] selector movement changes selection while Tab focus stays on play [then] focus and selector rings stay visible and distinct, [else stop]', async ({
	page
}) => {
	test.setTimeout(90_000);
	await boot(page, 'MORE');
	await loadDeckOneForSelectorFocus(page);

	await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
	for (let i = 0; i < 600; i++) {
		await page.keyboard.press('Tab');
		const onPlay = await page.evaluate(
			() =>
				document.activeElement?.matches('[data-testid="play-deck-1"]') === true &&
				(document.activeElement as HTMLElement).matches(':focus-visible')
		);
		if (onPlay) break;
	}
	await expect(page.locator('[data-testid="play-deck-1"]')).toBeFocused();

	await page.evaluate(() => window.__odjSelector!.start('deck.1'));
	await page.evaluate(() => window.__odjSelector!.move('right'));
	const selectedAfterMove = await syncSelectorRingToDomSelection(page);
	expect(selectedAfterMove, 'selector move should leave deck.1').not.toBe('deck.1');

	const focusedSelNode = await page.evaluate(() => {
		const el = document.activeElement as HTMLElement;
		return el.closest('[data-sel-node]')?.getAttribute('data-sel-node') ?? null;
	});
	expect(focusedSelNode, 'play button sits under a selector node').toBeTruthy();
	expect(selectedAfterMove).not.toBe(focusedSelNode);

	const snapshot = await readFocusSelectorCoexistence(page);
	assertFocusAndSelectorRingsDistinguishable(snapshot, 'selector move with play focused');
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

const focusedNodeId = (page: Page): Promise<string | null> =>
	page.evaluate(() => {
		const active = document.activeElement;
		if (active === null || active.getAttribute('role') !== 'slider') return null;
		return active.closest('[data-sel-node]')?.getAttribute('data-sel-node') ?? null;
	});

test('#13428: a fader re-mounted by the LESS swap keeps focus by selector id', async ({ page }) => {
	test.setTimeout(90_000);
	await boot(page, 'MORE');
	const fader = page.locator('[data-sel-node="mixer.ch1.fader"] [role="slider"]');
	await fader.focus();
	expect(await focusedNodeId(page)).toBe('mixer.ch1.fader');
	const firstFader = await fader.elementHandle();
	// A script click leaves focus on the fader, so only the re-render can drop it.
	await page.evaluate(() => {
		const buttons = document.querySelectorAll<HTMLButtonElement>('.deck-layout-toggle button');
		const less = [...buttons].find((button) => button.textContent?.trim() === 'LESS');
		if (less === undefined) throw new Error('no LESS button in .deck-layout-toggle');
		less.click();
	});
	await expect(page.locator('.perf-root')).toHaveClass(/deck-layout-less/);
	await expect.poll(() => focusedNodeId(page)).toBe('mixer.ch1.fader');
	const stillMounted = await firstFader!.evaluate((el) => el.isConnected);
	expect(stillMounted, 'the LESS swap re-mounted the fader').toBe(false);
});

test('[if] a deck is drilled into from the decks section [then] drill lands on deck.N then a deck part, [else stop]', async ({
	page
}) => {
	test.setTimeout(90_000);
	await boot(page, 'MORE');
	expect(await start(page, 'decks')).toBe('decks');
	const deckGroup = await drillIn(page);
	expect(deckGroup, 'first drill-in reaches a deck group').toMatch(/^deck\.[1-4]$/);
	const deckPart = await drillIn(page);
	const parts = await deckPartIds(page, Number(deckGroup.slice(5)));
	expect(parts, `second drill-in reaches a deck part, got ${deckPart}`).toContain(deckPart);
});

test('[if] each declared deck part is present in MORE mode [then] every inventory id is in the tree, [else stop]', async ({
	page
}) => {
	test.setTimeout(90_000);
	await boot(page, 'MORE');
	const ids = new Set(await nodeIds(page));
	expect(await deckParts(page)).toHaveLength(7);
	for (const deck of [1, 2, 3, 4] as const) {
		for (const partId of await deckPartIds(page, deck)) {
			expect(ids, `${partId} missing from selector snapshot`).toContain(partId);
			await expect(page.locator(`[data-sel-node="${partId}"]`)).toHaveCount(1);
		}
	}
});

test('[if] a deck part is disabled or removed by layout [then] its ids leave the snapshot, [else stop]', async ({
	page
}) => {
	test.setTimeout(90_000);
	await boot(page, 'LESS');
	const ids = await nodeIds(page);
	const hiddenParts = [...(await deckPartIds(page, 3)), ...(await deckPartIds(page, 4))];
	expect(
		ids.filter((id) => hiddenParts.includes(id)),
		'deck 3/4 parts absent in LESS snapshot'
	).toEqual([]);
	await expect(page.locator('.rb-deck[data-deck="3"]')).toHaveAttribute('data-sel-disabled', '');
	await expect(page.locator('.rb-deck[data-deck="4"]')).toHaveAttribute('data-sel-disabled', '');
	expect(ids).toContain('deck.1.header');
});
