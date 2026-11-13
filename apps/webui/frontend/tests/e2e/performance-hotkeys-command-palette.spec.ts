/**
 * Issue #12279 (A11Y-01): the Cmd-K command palette and the /performance global
 * shortcuts share the keyboard. While the palette's query input has focus, bare
 * performance letters type into the query and Space toggles no deck; once the
 * palette closes, the same keys on page chrome drive the decks again. Real key
 * presses at the real page, no mocked dispatches: deck state is read back from
 * the performance IPC. tests/unit/performance-hotkeys-command-palette.test.mjs
 * drives the same installed listener with stand-in targets. Uses the performance
 * fixture library (playwright.performance.config.ts) and keeps master muted.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

const API_BASE = process.env.PERFORMANCE_E2E_API_BASE ?? 'http://127.0.0.1:8686';
const MOD = process.platform === 'darwin' ? 'Meta' : 'Control';
const SETTLE_MS = 750;
/** C cue, V beat sync, K master tempo, P pitch range: each a deck shortcut on page chrome. */
const PERFORMANCE_LETTERS = ['k', 'v', 'c', 'p'];

type DeckOne = {
	playing: boolean;
	transport_pending: boolean;
	beat_sync_enabled: boolean;
	master_tempo_enabled: boolean;
};

async function firstOnDiskStableId(request: APIRequestContext): Promise<string> {
	const response = await request.get(`${API_BASE}/api/v1/tracks?limit=50&available=true`);
	expect(response.ok(), 'track listing must succeed').toBeTruthy();
	const payload = (await response.json()) as {
		items: { stable_id: string; file_exists: boolean }[];
	};
	const track = payload.items.find(
		(item) => item.file_exists && typeof item.stable_id === 'string' && item.stable_id.length > 0
	);
	expect(track, 'library must include at least one on-disk available track').toBeDefined();
	return track!.stable_id;
}

async function deckOne(page: Page): Promise<DeckOne> {
	return page.evaluate(() => {
		const deck = window.musicDjToolsPerformance!.query().decks[1];
		return {
			playing: deck.playing,
			transport_pending: deck.transport_pending,
			beat_sync_enabled: deck.beat_sync_enabled,
			master_tempo_enabled: deck.master_tempo_enabled
		};
	});
}

/** Boots /performance, mutes master and loads deck 1 paused, so Space has a deck to toggle. */
async function openWithPausedDeck(page: Page, request: APIRequestContext): Promise<void> {
	const stableId = await firstOnDiskStableId(request);
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
		timeout: 60_000
	});
	await page.evaluate(async (id) => {
		const perf = window.musicDjToolsPerformance!;
		await perf.dispatch({ type: 'master_volume', value: 0 });
		await perf.dispatch({ type: 'master_mute', muted: true, persist: false });
		await perf.dispatch({ type: 'load', deck: 1, stable_id: id });
	}, stableId);
	await page.waitForFunction(
		(id) => window.musicDjToolsPerformance?.query().decks[1].stable_id === id,
		stableId,
		{ timeout: 90_000 }
	);
	await page.evaluate(() =>
		window.musicDjToolsPerformance!.dispatch({ type: 'play', deck: 1, playing: false })
	);
	await expect
		.poll(async () => {
			const deck = await deckOne(page);
			return { playing: deck.playing, transport_pending: deck.transport_pending };
		})
		.toEqual({ playing: false, transport_pending: false });
}

/**
 * Opens the Cmd-K palette with its query input focused. The bar loads on demand
 * after the flags are read, so press until it opens rather than once.
 */
async function openPalette(page: Page): Promise<void> {
	await expect(async () => {
		await page.keyboard.press(`${MOD}+k`);
		await expect(page.locator('#cmdk2-list')).toBeVisible({ timeout: 1_000 });
	}).toPass({ timeout: 30_000 });
	await expect(page.getByTestId('command-bar-input')).toBeFocused();
}

/** Escape on an empty query closes the palette; focus then goes back to page chrome. */
async function closePaletteToChrome(page: Page): Promise<void> {
	const input = page.getByTestId('command-bar-input');
	await input.fill('');
	await page.keyboard.press('Escape');
	await expect(page.locator('#cmdk2-list')).toBeHidden();
	await page.evaluate(() => {
		if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
	});
	expect(await page.evaluate(() => document.activeElement === document.body)).toBe(true);
}

test.describe('Cmd-K palette and global performance shortcuts coexist (#12279)', () => {
	test.beforeEach(async ({ page, request }) => {
		await openWithPausedDeck(page, request);
	});

	test('[if] the palette input has focus [then] bare performance letters type into the query, [else stop].', async ({
		page
	}) => {
		const before = await deckOne(page);
		await openPalette(page);

		for (const key of PERFORMANCE_LETTERS) await page.keyboard.press(key);

		await expect(page.getByTestId('command-bar-input')).toHaveValue(PERFORMANCE_LETTERS.join(''));
		await page.waitForTimeout(SETTLE_MS);
		const after = await deckOne(page);
		expect(after.beat_sync_enabled, 'V in the query must not toggle beat sync').toBe(
			before.beat_sync_enabled
		);
		expect(after.master_tempo_enabled, 'K in the query must not toggle master tempo').toBe(
			before.master_tempo_enabled
		);
	});

	test('[if] the palette input has focus [then] Space types a space and does not toggle a deck, [else stop].', async ({
		page
	}) => {
		await openPalette(page);

		await page.keyboard.press('Space');

		await expect(page.getByTestId('command-bar-input')).toHaveValue(' ');
		await page.waitForTimeout(SETTLE_MS);
		const deck = await deckOne(page);
		expect({ playing: deck.playing, transport_pending: deck.transport_pending }).toEqual({
			playing: false,
			transport_pending: false
		});
	});

	test('[if] the palette closes [then] Space and K on page chrome drive deck 1 again, [else stop].', async ({
		page
	}) => {
		await openPalette(page);
		await page.keyboard.press('Space');
		await closePaletteToChrome(page);
		const before = await deckOne(page);
		expect(before.playing).toBe(false);

		await page.keyboard.press('k');
		await expect
			.poll(async () => (await deckOne(page)).master_tempo_enabled, { timeout: 15_000 })
			.toBe(!before.master_tempo_enabled);

		await page.keyboard.press('Space');
		await expect.poll(async () => (await deckOne(page)).playing, { timeout: 30_000 }).toBe(true);
		await page.evaluate(() =>
			window.musicDjToolsPerformance!.dispatch({ type: 'play', deck: 1, playing: false })
		);
	});
});
