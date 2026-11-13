// requirement: USBPLAY-14
// requirement: USBPLAY-13990
/**
 * USBPLAY-14 in the browser: a background library refresh of an open stick pane
 * re-asks GET /api/v1/usb/volumes/{id}/library and replaces pane rows in place.
 *
 * Volumes and stick libraries are fulfilled in the page through Playwright
 * routes; the daemon's real routes are not patched. refreshUsbPane,
 * BrowserPanel's stick branch, and the track table are the real app. A library
 * play POST triggers _requestLibraryRefresh the same way a mid-set track edit
 * would.
 *
 * - [if] a stick pane is open with an initial export response [then] it lists
 *   that export's tracks, not an empty pane
 * - [if] the next refresh response has changed rows [then] the pane replaces
 *   rows instead of appending or keeping stale titles
 * - [if] the refresh response fails [then] the pane greys in place and names
 *   stick removed in the header
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import { spendBootLanding } from './support/boot-landing';
import {
	STICK_VOLUME_ID,
	STICK_VOLUME_UUID,
	USB_STICK_LIBRARY_INITIAL,
	USB_STICK_LIBRARY_REPLACED,
	USB_STICK_VOLUMES_LIST,
	usbStickNotMountedDetail
} from './support/usb-stick-pane-refresh-fixture';

const LIBRARY_PATH = '**/api/v1/usb/volumes/*/library';
const ROW = '[data-testid="browser-panel"] [data-testid="track-row"][data-stable-id^="usb-"]';

async function openStickAllTracks(page: Page): Promise<void> {
	// Stick panes open through browser_select_playlist, which needs a live
	// performance command session; Library mode mounts the same BrowserPanel but
	// has no session, so Gig's muted performance route is the browser path.
	await page.addInitScript(() => sessionStorage.setItem('odj.enrich-card.hidden', '1'));
	await page.goto('/performance?muted=1');
	await expect(page.getByTestId('browser-panel')).toBeVisible({ timeout: 60_000 });
	await page.getByTestId('library-source-usbs').click();
	const stickRow = page.getByTestId('usb-source-row').filter({ hasText: 'E2E STICK' });
	await expect(stickRow).toBeVisible({ timeout: 15_000 });
	await stickRow.click();
	const allTracks = page
		.getByTestId('usb-stick-node')
		.filter({ hasText: 'All tracks' })
		.and(page.locator(`[data-pane-id="usbpl:${STICK_VOLUME_UUID}:all"]`));
	await expect(allTracks).toBeVisible({ timeout: 15_000 });
	const libraryRead = page.waitForResponse(
		(response) =>
			response.request().method() === 'GET' &&
			new URL(response.url()).pathname.includes('/api/v1/usb/volumes/') &&
			new URL(response.url()).pathname.endsWith('/library')
	);
	await allTracks.click();
	await libraryRead;
	await expect(page.locator('.pane-header')).toContainText('E2E STICK', { timeout: 15_000 });
	await expect(page.locator(ROW)).toHaveCount(3, { timeout: 15_000 });
}

async function installVolumeListing(page: Page): Promise<void> {
	await page.route('**/api/v1/usb/volumes', async (route) => {
		const url = new URL(route.request().url());
		if (url.pathname !== '/api/v1/usb/volumes') {
			await route.continue();
			return;
		}
		await route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify(USB_STICK_VOLUMES_LIST)
		});
	});
}

async function triggerLibraryRefresh(request: APIRequestContext): Promise<void> {
	const list = await request.get('/api/v1/tracks?limit=1');
	expect(list.ok()).toBe(true);
	const body = await list.json();
	const stableId = body.items?.[0]?.stable_id as string | undefined;
	expect(typeof stableId === 'string' && stableId.length > 0, JSON.stringify(body)).toBe(true);
	const playId = `e2e-usb-refresh-${Date.now()}`;
	const posted = await request.post(`/api/v1/tracks/${stableId}/plays`, {
		data: { play_id: playId, audible_s: 60 }
	});
	expect(posted.ok(), await posted.text()).toBe(true);
}

test.describe('USB stick pane refresh replaces rows in the browser', () => {
	test.describe.configure({ timeout: 60_000 });

	test.beforeEach(async ({ page }) => {
		await spendBootLanding(page);
		await installVolumeListing(page);
	});

	test('[if] a stick pane is open with an initial export response [then] it lists that export tracks', async ({
		page
	}) => {
		let libraryReads = 0;
		await page.route(LIBRARY_PATH, async (route) => {
			libraryReads += 1;
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify(USB_STICK_LIBRARY_INITIAL)
			});
		});
		await openStickAllTracks(page);
		await expect(page.locator(ROW).filter({ hasText: 'Initial Alpha' })).toHaveCount(1);
		await expect(page.locator(ROW).filter({ hasText: 'Initial Beta' })).toHaveCount(1);
		expect(
			libraryReads,
			'the pane must read the backend for the first paint'
		).toBeGreaterThanOrEqual(1);
	});

	test('[if] the next refresh response has changed rows [then] the pane replaces rows instead of appending', async ({
		page,
		request
	}) => {
		let libraryReads = 0;
		let refreshPhase = false;
		await page.route(LIBRARY_PATH, async (route) => {
			libraryReads += 1;
			const body = refreshPhase ? USB_STICK_LIBRARY_REPLACED : USB_STICK_LIBRARY_INITIAL;
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify(body)
			});
		});
		await openStickAllTracks(page);
		refreshPhase = true;
		await expect(page.locator(ROW).filter({ hasText: 'Initial Beta' })).toHaveCount(1);
		await triggerLibraryRefresh(request);
		await expect
			.poll(() => libraryReads, {
				message: 'a library refresh must re-ask the stick library',
				timeout: 15_000
			})
			.toBeGreaterThanOrEqual(2);
		await expect(page.locator(ROW)).toHaveCount(4, { timeout: 15_000 });
		await expect(page.locator(ROW).filter({ hasText: 'Initial Beta' })).toHaveCount(0);
		await expect(page.locator(ROW).filter({ hasText: 'Replaced Delta' })).toHaveCount(1);
	});

	test('[if] the refresh response fails [then] the pane greys in place and names stick removed', async ({
		page,
		request
	}) => {
		let libraryReads = 0;
		let refreshPhase = false;
		await page.route(LIBRARY_PATH, async (route) => {
			libraryReads += 1;
			if (!refreshPhase) {
				await route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify(USB_STICK_LIBRARY_INITIAL)
				});
				return;
			}
			await route.fulfill({
				status: 404,
				contentType: 'application/json',
				body: JSON.stringify(usbStickNotMountedDetail())
			});
		});
		await openStickAllTracks(page);
		refreshPhase = true;
		await triggerLibraryRefresh(request);
		await expect.poll(() => libraryReads, { timeout: 15_000 }).toBeGreaterThanOrEqual(2);
		await expect(page.locator('.pane-header')).toContainText('stick removed');
		await expect(page.locator(ROW)).toHaveCount(3);
		await expect(page.locator(ROW).filter({ hasText: 'Initial Alpha' })).toHaveCount(1);
		// Pulled-stick rows use awaiting_volume (not the broken-link class); deck
		// load is refused as Stick removed per USBPLAY-09.
		await page.locator(ROW).filter({ hasText: 'Initial Alpha' }).click({ button: 'right' });
		const loadDeck1 = page.getByTestId('context-menu-load-deck-1');
		await expect(loadDeck1).toHaveAttribute('aria-disabled', 'true');
		await expect(loadDeck1).toHaveAttribute('title', /missing|Stick removed/i);
	});
});
