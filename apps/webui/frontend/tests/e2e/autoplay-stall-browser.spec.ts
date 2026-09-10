import { expect, test, type Page } from '@playwright/test';

/**
 * PLAY-08's stall banner, in a real browser (issue #1640).
 *
 * WHAT THIS PROVES that the unit suite cannot. The SSR render test asserts
 * MARKUP; this asserts that the element is VISIBLE, that it appears without a
 * reload after the state changes, that its toggle works, and that neither the
 * banner nor its expanded list covers the controls underneath. All four are
 * layout and runtime facts, and all four were named as gaps by review
 * (r3973806306, r3974734057).
 *
 * WHAT IT DOES NOT PROVE, stated rather than glossed: the stall here is raised
 * by calling the real store, not by starving a real playlist of playable
 * audio. Driving a genuine exhaustion needs a deck loaded, playing and inside
 * its last 16 seconds with a spent feed underneath, which this fixture library
 * cannot arrange without fabricating most of it anyway. That half - controller
 * reaches a terminal branch and records a stall - is driven for real against
 * the real controller in tests/unit/autoplay-stall-persistence.test.mjs. The
 * seam between the two is the store, and both sides touch the real one.
 *
 * Runs under playwright.autoplay-stall-gate.config.ts (real backend, real
 * throwaway fixture library), not the default config.
 */

const BANNER = '[data-testid="autoplay-stall-banner"]';
const TRACKS = '[data-testid="autoplay-stall-tracks"]';

/**
 * Raise a stall through the app's OWN module instance.
 *
 * Vite serves source modules from the same registry the app loaded, so this
 * import is the same singleton the mounted component reads - not a second copy
 * that would leave the banner untouched and the test passing for the wrong
 * reason.
 */
async function raiseStall(page: Page, blockedCount: number): Promise<void> {
	await page.evaluate(async (count) => {
		const store = await import(
			new URL('/src/lib/rb/autoplay-stall.svelte.ts', location.href).href
		);
		const pure = await import(new URL('/src/lib/rb/autoplay-stall.ts', location.href).href);
		store.raiseAutoPlayStall(
			pure.describeAutoPlayStall({
				reason: 'missing-audio',
				source_stable_id: 'browser-gate-source',
				blocked: Array.from({ length: count }, (_, index) => ({
					stable_id: `gone-${index}`,
					key: '8A',
					bpm: 124,
					file_exists: false,
					title: `Missing Track Number ${index}`,
					artist: `Artist ${index}`
				}))
			})
		);
	}, blockedCount);
}

async function clearStall(page: Page): Promise<void> {
	await page.evaluate(async () => {
		const store = await import(
			new URL('/src/lib/rb/autoplay-stall.svelte.ts', location.href).href
		);
		store.clearAutoPlayStall();
	});
}

/** The banner must not be pinned over the topbar it is meant to sit under. */
async function topbarBottom(page: Page): Promise<number> {
	const box = await page.locator('.rb-topbar').first().boundingBox();
	expect(box, 'the topbar did not render: nothing below is measurable').not.toBeNull();
	return box!.y + box!.height;
}

test.beforeEach(async ({ page }) => {
	test.setTimeout(120_000);
	await page.goto('/performance');
	// Past the preflight "Starting up" screen: the topbar only exists once the
	// real route mounted, so this is the readiness signal, not a fixed sleep.
	await expect(page.locator('.rb-topbar').first()).toBeVisible({ timeout: 90_000 });
	await clearStall(page);
});

test('a healthy performance page carries no stall banner', async ({ page }) => {
	// The control for every assertion below: if the banner were always in the
	// DOM, "it appeared" would prove nothing.
	await expect(page.locator(BANNER)).toHaveCount(0);
});

test('a stall raised after mount appears, visibly, under the topbar', async ({ page }) => {
	await raiseStall(page, 3);
	const banner = page.locator(BANNER);
	// toBeVisible, not toHaveCount: an element clipped to zero height by the
	// topbar's `overflow: hidden`, or painted behind the waveform stack, is
	// present in the markup and useless to the operator.
	await expect(banner).toBeVisible();
	await expect(banner).toContainText('all 3 remaining playlist tracks');
	await expect(banner).toContainText('Relink or re-download');

	const box = await banner.boundingBox();
	expect(box, 'a visible banner with no box is a contradiction').not.toBeNull();
	expect(box!.height).toBeGreaterThan(0);
	expect(box!.y).toBeGreaterThanOrEqual(await topbarBottom(page) - 1);
});

test('the banner leaves the DOM when the stall is retired', async ({ page }) => {
	await raiseStall(page, 2);
	await expect(page.locator(BANNER)).toBeVisible();
	await clearStall(page);
	// Gone, not merely hidden: a hidden banner still occupies the reader's
	// accessibility tree and would keep announcing a stop that is over.
	await expect(page.locator(BANNER)).toHaveCount(0);
});

test('the track list opens on click, and never covers the control that closes it', async ({
	page
}) => {
	await raiseStall(page, 15);
	const toggle = page.getByRole('button', { name: /Show the 15 tracks/ });
	await expect(toggle).toBeVisible();
	await expect(page.locator(TRACKS)).toHaveCount(0);

	await toggle.click();
	const list = page.locator(TRACKS);
	await expect(list).toBeVisible();
	await expect(list).toContainText('Artist 0 - Missing Track Number 0');
	// The cap is 12 of 15, and the remainder must be stated rather than
	// silently dropped.
	await expect(list.locator('li')).toHaveCount(13);
	await expect(list).toContainText('and 3 more');

	// The r3974518073 failure: a list positioned independently of the banner
	// covering the toggle. Playwright's click actionability re-checks hit-testing, so
	// a covered control fails here rather than in front of the operator.
	const hide = page.getByRole('button', { name: /Hide the 15 tracks/ });
	await expect(hide).toBeVisible();
	await hide.click();
	await expect(page.locator(TRACKS)).toHaveCount(0);
});

test('the banner does not obscure the deck controls beneath it', async ({ page }) => {
	const deckControl = page.locator('[data-testid="grid-adjust-deck-1"]').first();
	const before = await deckControl.boundingBox();
	test.skip(before === null, 'deck 1 grid control is not on screen at this viewport');

	await raiseStall(page, 15);
	await expect(page.locator(BANNER)).toBeVisible();
	await page.getByRole('button', { name: /Show the 15 tracks/ }).click();
	await expect(page.locator(TRACKS)).toBeVisible();

	// Still where it was, and still the topmost element at its own centre: the
	// banner is fixed and outside the grid, so it must not have reflowed the
	// decks NOR be painted over them.
	const after = await deckControl.boundingBox();
	expect(after, 'the deck control vanished when the banner appeared').not.toBeNull();
	expect(Math.round(after!.y)).toBe(Math.round(before!.y));
	const topmostIsDeckControl = await deckControl.evaluate((element) => {
		const box = element.getBoundingClientRect();
		const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
		return element === hit || element.contains(hit);
	});
	expect(topmostIsDeckControl, 'the stall list is painted over a deck control').toBe(true);
});
