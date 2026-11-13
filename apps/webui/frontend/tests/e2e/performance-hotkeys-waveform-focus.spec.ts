/**
 * Issue #12274 (A11Y-02): the pointer-only waveform focus exception, against
 * the running /performance page. WaveRow.svelte's seek canvas is
 * role="slider" tabindex="-1" data-hotkey-pointer-only and owns no keys; a
 * click on an EMPTY deck's canvas skips preventDefault, so the browser moves
 * real focus onto it. Global hotkeys must stay live there, while a focused
 * native button keeps its own Space and a text entry keeps its letters.
 * tests/unit/performance-hotkeys-waveform-focus.test.mjs drives the same
 * installed listener with stand-in targets; only a browser proves real focus.
 * Uses the performance fixture library (playwright.performance.config.ts) and
 * keeps master muted throughout.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

const API_BASE = process.env.PERFORMANCE_E2E_API_BASE ?? 'http://127.0.0.1:8686';
const SETTLE_MS = 750;
const EMPTY_DECK_CANVAS = '.rb-waverow[data-deck="2"] canvas.wave-seek-canvas';

type KeyProbe = {
	clicks: number;
	keydowns: { key: string; tag: string; defaultPrevented: boolean }[];
};

type DeckOne = { playing: boolean; master_tempo_enabled: boolean };

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
		return { playing: deck.playing, master_tempo_enabled: deck.master_tempo_enabled };
	});
}

async function pauseDeckOne(page: Page): Promise<void> {
	await page.evaluate(() =>
		window.musicDjToolsPerformance!.dispatch({ type: 'play', deck: 1, playing: false })
	);
}

async function readProbe(page: Page): Promise<KeyProbe> {
	return page.evaluate(
		() => (window as unknown as { __waveFocusProbe: KeyProbe }).__waveFocusProbe
	);
}

async function focusBody(page: Page): Promise<void> {
	await page.evaluate(() => {
		if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
	});
	expect(await page.evaluate(() => document.activeElement === document.body)).toBe(true);
}

/**
 * Boots /performance, mutes master, loads deck 1 paused (deck 2 stays empty),
 * and mounts one real native button and one real text input. A window BUBBLE
 * listener, added after the app's capture listener, records whether each
 * keydown reached the page default-prevented.
 */
async function openWithPausedDeckAndProbes(page: Page, request: APIRequestContext): Promise<void> {
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
	await pauseDeckOne(page);
	await expect.poll(async () => (await deckOne(page)).playing).toBe(false);
	await expect(page.locator(EMPTY_DECK_CANVAS)).toHaveAttribute('data-hotkey-pointer-only', '');

	await page.evaluate(() => {
		const probe: KeyProbe = { clicks: 0, keydowns: [] };
		(window as unknown as { __waveFocusProbe: KeyProbe }).__waveFocusProbe = probe;
		const host = document.createElement('div');
		host.style.cssText = 'position:fixed;left:8px;bottom:8px;z-index:2147483647';
		const button = document.createElement('button');
		button.type = 'button';
		button.textContent = 'Waveform focus probe';
		button.dataset.testid = 'wave-focus-button';
		button.addEventListener('click', () => {
			probe.clicks += 1;
		});
		const input = document.createElement('input');
		input.type = 'text';
		input.dataset.testid = 'wave-focus-input';
		host.append(button, input);
		document.body.append(host);
		window.addEventListener('keydown', (event) => {
			const tag = event.target instanceof Element ? event.target.tagName : 'NONE';
			probe.keydowns.push({ key: event.key, tag, defaultPrevented: event.defaultPrevented });
		});
	});
}

test.describe('hotkeys after a pointer click focuses the waveform seek canvas (#12274)', () => {
	test.beforeEach(async ({ page, request }) => {
		await openWithPausedDeckAndProbes(page, request);
	});

	test('[if] a pointer click focuses the waveform seek canvas [then] Space still toggles deck transport, [else stop].', async ({
		page
	}) => {
		const canvas = page.locator(EMPTY_DECK_CANVAS);
		// A real pointer click on the empty deck: its pointerdown skips
		// preventDefault, so the browser's own focus-on-click lands here.
		await canvas.click({ force: true });
		await expect(canvas, 'the click must move real focus onto the canvas').toBeFocused();

		await page.keyboard.press('Space');

		await expect.poll(async () => (await deckOne(page)).playing, { timeout: 30_000 }).toBe(true);
		const probe = await readProbe(page);
		expect(probe.keydowns).toEqual([{ key: ' ', tag: 'CANVAS', defaultPrevented: true }]);
		await expect(canvas, 'a hotkey must not need focus moved off the canvas').toBeFocused();
		await pauseDeckOne(page);
	});

	test('[if] a focused native button receives Space [then] it activates itself and transport stays paused, [else stop].', async ({
		page
	}) => {
		const button = page.getByTestId('wave-focus-button');
		await button.focus();
		await expect(button).toBeFocused();

		await page.keyboard.press('Space');

		await expect.poll(async () => (await readProbe(page)).clicks).toBe(1);
		await page.waitForTimeout(SETTLE_MS);
		const probe = await readProbe(page);
		expect(probe.keydowns).toEqual([{ key: ' ', tag: 'BUTTON', defaultPrevented: false }]);
		expect((await deckOne(page)).playing).toBe(false);
	});

	test('[if] a text entry receives a bare letter [then] it types and no performance command dispatches, [else stop].', async ({
		page
	}) => {
		const before = await deckOne(page);
		const input = page.getByTestId('wave-focus-input');
		await input.focus();
		await expect(input).toBeFocused();

		// K is the recent deck's master tempo toggle on page chrome.
		await page.keyboard.press('k');

		await expect(input).toHaveValue('k');
		await page.waitForTimeout(SETTLE_MS);
		expect(await deckOne(page), 'typing k must not toggle master tempo').toEqual(before);
		expect((await readProbe(page)).keydowns).toEqual([
			{ key: 'k', tag: 'INPUT', defaultPrevented: false }
		]);

		// Positive control: the same bare K on page chrome does dispatch, so the
		// pass above is the text-entry gate's doing.
		await focusBody(page);
		await page.keyboard.press('k');
		await expect
			.poll(async () => (await deckOne(page)).master_tempo_enabled, { timeout: 30_000 })
			.toBe(!before.master_tempo_enabled);
		await page.evaluate(
			(enabled) =>
				window.musicDjToolsPerformance!.dispatch({ type: 'master_tempo', deck: 1, enabled }),
			before.master_tempo_enabled
		);
	});
});
