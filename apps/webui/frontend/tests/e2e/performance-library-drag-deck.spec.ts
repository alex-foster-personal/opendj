/**
 * DECKUX-21 / LIBUX-21: Chromium drag-to-deck load and Space must not scroll
 * the library. Uses the performance fixture library (playwright.performance.config.ts).
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

const API_BASE = process.env.PERFORMANCE_E2E_API_BASE ?? 'http://127.0.0.1:8686';
const TRACK_ROW = '[data-testid="track-row"]';
const TRACK_STABLE_MIME = 'application/x-mdt-stable-id';

/** Optional row probes; benign 404s on other tracks must not mask failures on the dragged row. */
const OPTIONAL_ROW_PROBE_PATTERNS: readonly RegExp[] = [
	/\/api\/v1\/tracks\/[^/]+\/artwork(\?|$)/,
	/\/api\/v1\/tracks\/[^/]+\/stems(\?|$)/
];

function isIgnorableOptionalRowProbe(url: string, draggedStableId: string): boolean {
	if (!OPTIONAL_ROW_PROBE_PATTERNS.some((pattern) => pattern.test(url))) return false;
	const match = url.match(/\/api\/v1\/tracks\/([^/]+)\//);
	if (match?.[1] === draggedStableId) return false;
	return true;
}

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

/**
 * A REAL pointer drag: Playwright's mouse down / move / up on Chromium goes
 * through the browser's own hit-testing and native drag machinery, so the row
 * must really start a drag and the deck must really be the element under the
 * pointer at drop. Capture-phase listeners record that the dragstart on the
 * row and the drop on the deck were browser-generated (`isTrusted`), which a
 * synthetic `dispatchEvent` can never be. The WebKit path (custom MIME hidden
 * during dragover, in-app drag state carries acceptance) is pinned by
 * tests/unit/track-drag-drop.test.mjs.
 */
async function dragRowToDeck(
	page: Page,
	stableId: string,
	deckId: number
): Promise<{ trustedDragStart: boolean; trustedDrop: boolean; payloadOnDrop: string }> {
	await page.evaluate(
		({ sid, deck, mime }) => {
			const probe = { trustedDragStart: false, trustedDrop: false, payloadOnDrop: '' };
			(window as unknown as { __dragDeckProbe: typeof probe }).__dragDeckProbe = probe;
			document.addEventListener(
				'dragstart',
				(event) => {
					const row = (event.target as Element | null)?.closest?.('[data-testid="track-row"]');
					if (row?.getAttribute('data-stable-id') === sid && event.isTrusted) {
						probe.trustedDragStart = true;
					}
				},
				{ capture: true }
			);
			document.addEventListener(
				'drop',
				(event) => {
					const target = (event.target as Element | null)?.closest?.('section.rb-deck');
					if (target?.getAttribute('data-deck') === String(deck) && event.isTrusted) {
						probe.trustedDrop = true;
						probe.payloadOnDrop = event.dataTransfer?.getData(mime) ?? '';
					}
				},
				{ capture: true }
			);
		},
		{ sid: stableId, deck: deckId, mime: TRACK_STABLE_MIME }
	);
	const source = page.locator(`${TRACK_ROW}[data-stable-id="${stableId}"] .c-artist`);
	const target = page.locator(`section.rb-deck[data-deck="${deckId}"]`);
	await expect(source).toBeVisible();
	await expect(target).toBeVisible();
	await source.dragTo(target);
	return page.evaluate(
		() =>
			(
				window as unknown as {
					__dragDeckProbe: { trustedDragStart: boolean; trustedDrop: boolean; payloadOnDrop: string };
				}
			).__dragDeckProbe
	);
}

test('performance: drag library row onto deck loads track and Space toggles play without scrolling library', async ({
	page,
	request
}) => {
	test.setTimeout(120_000);
	const stableId = await firstOnDiskStableId(request);
	const pageErrors: string[] = [];
	page.on('pageerror', (err) => pageErrors.push(err.message));
	page.on('console', (msg) => {
		if (msg.type() !== 'error') return;
		const text = msg.text();
		if (
			text.startsWith('Failed to load resource:') &&
			isIgnorableOptionalRowProbe(msg.location().url, stableId)
		) {
			return;
		}
		pageErrors.push(text);
	});
	page.on('response', (response) => {
		if (response.status() < 400) return;
		if (isIgnorableOptionalRowProbe(response.url(), stableId)) return;
		pageErrors.push(`http ${response.status()}: ${response.url()}`);
	});
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
		timeout: 60_000
	});

	const row = page.locator(`${TRACK_ROW}[data-stable-id="${stableId}"]`);
	await row.scrollIntoViewIfNeeded();
	await expect(row).toBeVisible({ timeout: 60_000 });
	await row.click();

	const gesture = await dragRowToDeck(page, stableId, 1);
	expect(gesture.trustedDragStart, 'a real pointer drag must start on the library row').toBe(true);
	expect(gesture.trustedDrop, 'the real drag must drop on deck 1').toBe(true);
	expect(gesture.payloadOnDrop).toBe(stableId);

	await page.waitForFunction(
		(id) => window.musicDjToolsPerformance?.query().decks[1].stable_id === id,
		stableId,
		{ timeout: 90_000 }
	);

	const tableWrap = page.locator('.table-wrap');
	const scrollBefore = await tableWrap.evaluate((el) => el.scrollTop);
	await page.keyboard.press('Space');
	const scrollAfter = await tableWrap.evaluate((el) => el.scrollTop);
	expect(scrollAfter).toEqual(scrollBefore);

	await page.waitForFunction(
		() => window.musicDjToolsPerformance?.query().decks[1].playing === true,
		undefined,
		{ timeout: 30_000 }
	);
	expect(pageErrors, 'drag and Space must not surface page or console errors').toEqual([]);
});

function assertAllDecksUnloadedAndStopped(
	decks: Record<number, { stable_id: string | null; playing: boolean }>
): void {
	for (const deck of Object.values(decks)) {
		expect(deck.stable_id, 'deck must be unloaded before Space no-op check').toBeNull();
		expect(deck.playing, 'deck must be stopped before Space no-op check').toBe(false);
	}
}

test('performance: Space on library with no loaded deck does not scroll the table', async ({
	page
}) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
		timeout: 60_000
	});
	const tableWrap = page.locator('.table-wrap');
	await expect(tableWrap).toBeVisible({ timeout: 60_000 });

	const trackRow = page.locator(`${TRACK_ROW}[tabindex="0"]`).first();
	await expect(trackRow).toBeVisible({ timeout: 60_000 });

	const beforeSpace = await page.evaluate(() => window.musicDjToolsPerformance!.query());
	assertAllDecksUnloadedAndStopped(beforeSpace.decks);

	await trackRow.focus();
	await expect(trackRow).toBeFocused();

	const scrollBefore = await tableWrap.evaluate((el) => el.scrollTop);
	await page.keyboard.press('Space');
	await expect
		.poll(async () => tableWrap.evaluate((el) => el.scrollTop), { timeout: 5_000 })
		.toBe(scrollBefore);

	const afterSpace = await page.evaluate(() => window.musicDjToolsPerformance!.query());
	assertAllDecksUnloadedAndStopped(afterSpace.decks);
});
