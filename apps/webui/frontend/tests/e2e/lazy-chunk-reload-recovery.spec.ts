/**
 * A lazy chunk that fails to load is recovered by a Reload the user clicks,
 * never by retrying in the same document (CHROME-07, issue #3886).
 *
 * Codex P2 4130011276 on PR #3896: the browser keeps a failed module fetch in
 * its module map, so a second import() of the same URL in the same document
 * rejects again without touching the network (routes/+layout.svelte measured
 * this for the setup wizard). The loaders here used to promise "close and
 * reopen to retry", which could never work; the round-8 harness hid that by
 * resolving the retry through an import map to a DIFFERENT URL.
 *
 * This drives the real /performance page (root suite: real engine, fixture
 * library, Vite dev server). page.route aborts the real module URL of each
 * lazy chunk, so the SAME URL fails. It covers the four lazy loads this PR
 * added: the MIDI drawer (MidiPanelLoader), the drawer's device list and
 * learn-log pop-out (MidiPanel), and the track row's Show in playlists and
 * Relocate popovers (TrackRowPopovers).
 *
 * [if] a failed chunk shows no error, or no Reload action [then] stop.
 * [if] the page reloads before the user clicks Reload [then] stop.
 * [if] clicking Reload does not bring the component back in a fresh document
 *   once the chunk can be fetched [then] stop.
 * negative control: in the same document, a re-import of the exact failed
 *   URL still rejects and puts no request on the wire, after the route allows
 *   it again. That is why recovery must be a fresh document.
 */
// requirement: CHROME-07
import { expect, test, type Page } from '@playwright/test';

type Failing = { urls: string[]; allow(): Promise<void> };

/** Abort every request for the named source files until allow() is called. */
async function failChunks(page: Page, files: readonly string[]): Promise<Failing> {
	const urls: string[] = [];
	const matches = (url: URL) => files.some((f) => url.pathname.endsWith(`/${f}`));
	const handler = (route: import('@playwright/test').Route) => {
		urls.push(route.request().url());
		return route.abort('failed');
	};
	await page.route(matches, handler);
	return { urls, allow: () => page.unroute(matches, handler) };
}

/** Requests the page makes for these files from now on, allowed or not. */
function watchRequests(page: Page, files: readonly string[]): string[] {
	const seen: string[] = [];
	page.on('request', (r) => {
		const path = new URL(r.url()).pathname;
		if (files.some((f) => path.endsWith(`/${f}`))) seen.push(r.url());
	});
	return seen;
}

async function openPerformance(page: Page): Promise<void> {
	test.setTimeout(150_000);
	await page.setViewportSize({ width: 1440, height: 1000 });
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
		timeout: 60_000
	});
}

/** A marker that only survives while the document does. */
const markDocument = (page: Page) =>
	page.evaluate(() => ((window as unknown as { __sameDoc?: boolean }).__sameDoc = true));
const sameDocument = (page: Page) =>
	page.evaluate(() => (window as unknown as { __sameDoc?: boolean }).__sameDoc === true);

/** The exact failed URL, re-imported in this document. */
const reimport = (page: Page, url: string) =>
	page.evaluate((u) => import(/* @vite-ignore */ u).then(() => 'loaded', () => 'rejected'), url);

async function reloadFromAlert(page: Page, alert: import('@playwright/test').Locator): Promise<void> {
	// Nothing reloads on its own: the document outlives the error.
	await page.waitForTimeout(500);
	expect(await sameDocument(page), 'the page reloaded before the user asked').toBe(true);
	await Promise.all([page.waitForEvent('load'), alert.getByRole('button', { name: 'Reload' }).click()]);
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
		timeout: 60_000
	});
	expect(await sameDocument(page), 'Reload must load a fresh document').toBe(false);
}

const openMidi = (page: Page) => page.getByRole('button', { name: 'Open MIDI panel', exact: true }).click();
const drawer = (page: Page) => page.getByRole('dialog', { name: 'MIDI devices and learn log' });

test('a MIDI drawer chunk that fails offers Reload, and only a fresh document recovers it', async ({ page }) => {
	await openPerformance(page);
	const file = 'MidiPanel.svelte';
	const failing = await failChunks(page, [file]);
	await markDocument(page);

	await openMidi(page);
	const alert = page.getByTestId('midi-panel-load-error');
	await expect(alert).toContainText('MIDI panel failed to load');
	await expect(alert).toHaveAttribute('role', 'alert');
	await expect(alert.getByRole('button', { name: 'Reload' })).toBeVisible();
	await expect(drawer(page)).toHaveCount(0);
	expect(failing.urls.length, 'the real chunk URL was requested and failed').toBeGreaterThan(0);

	// Negative control: the chunk is fetchable again, yet this document cannot
	// get it. Close and reopen still shows the error, and a re-import of the
	// same URL rejects without a request.
	await failing.allow();
	const after = watchRequests(page, [file]);
	await alert.getByRole('button', { name: 'Close' }).click();
	await expect(alert).toHaveCount(0);
	await openMidi(page);
	await expect(alert).toBeVisible();
	expect(await reimport(page, failing.urls[0])).toBe('rejected');
	expect(after, 'the module map answered; nothing went on the wire').toEqual([]);

	await reloadFromAlert(page, alert);
	await openMidi(page);
	await expect(drawer(page)).toBeVisible({ timeout: 15_000 });
	await expect(page.getByTestId('midi-panel-load-error')).toHaveCount(0);
});

test('the drawer device list and learn-log pop-out offer Reload, which brings both back', async ({ page }) => {
	await openPerformance(page);
	const failing = await failChunks(page, ['MidiDeviceList.svelte', 'MidiLearnLogPopout.svelte']);
	await markDocument(page);

	await openMidi(page);
	await expect(drawer(page)).toBeVisible({ timeout: 15_000 });
	const devices = drawer(page).getByRole('alert').filter({ hasText: 'Devices failed to load' });
	await expect(devices.getByRole('button', { name: 'Reload' })).toBeVisible();
	await drawer(page).getByRole('button', { name: 'pop out' }).click();
	const popout = page.getByRole('alert').filter({ hasText: 'Learn log pop-out failed to load' });
	await expect(popout.getByRole('button', { name: 'Reload' })).toBeVisible();
	await popout.getByRole('button', { name: 'Close' }).click();
	await expect(popout).toHaveCount(0);

	// Same document, chunks fetchable again: closing and reopening does not help.
	await failing.allow();
	await drawer(page).getByRole('button', { name: 'close', exact: true }).click();
	await openMidi(page);
	await expect(devices).toBeVisible();

	await reloadFromAlert(page, devices);
	await openMidi(page);
	await expect(drawer(page).locator('.device-list')).toBeVisible({ timeout: 15_000 });
	await drawer(page).getByRole('button', { name: 'pop out' }).click();
	await expect(page.getByRole('log', { name: 'MIDI learn log' })).toBeVisible();
	await expect(page.getByRole('alert').filter({ hasText: 'failed to load' })).toHaveCount(0);
});

test('track-row popovers that fail offer Reload, which brings them back', async ({ page }) => {
	await openPerformance(page);
	const row = page.locator('[data-testid="track-row"]').first();
	await expect(row).toBeVisible({ timeout: 60_000 });
	const stableId = await row.getAttribute('data-stable-id');
	const target = page.locator(`[data-testid="track-row"][data-stable-id="${stableId}"]`);
	const pick = async (item: string) => {
		await target.click({ button: 'right' });
		await page.getByRole('menuitem', { name: item }).click();
	};
	const failing = await failChunks(page, ['TrackPlaylistsPopover.svelte', 'RelocatePopover.svelte']);
	await markDocument(page);

	for (const [item, label] of [
		['Show in playlists', 'Show in playlists failed to load'],
		['Relocate', 'Relocate failed to load']
	] as const) {
		await pick(item);
		const alert = page.getByRole('alert').filter({ hasText: label });
		await expect(alert.getByRole('button', { name: 'Reload' })).toBeVisible();
		await alert.getByRole('button', { name: 'Close' }).click();
		await expect(alert).toHaveCount(0);
	}

	// Same document, chunks fetchable again: the next pick still fails.
	await failing.allow();
	await pick('Show in playlists');
	const playlistsAlert = page.getByRole('alert').filter({ hasText: 'Show in playlists failed to load' });
	await expect(playlistsAlert).toBeVisible();

	await reloadFromAlert(page, playlistsAlert);
	await expect(target).toBeVisible({ timeout: 60_000 });
	await pick('Show in playlists');
	await expect(page.getByTestId('track-playlists-menu')).toBeVisible({ timeout: 15_000 });
	await page.keyboard.press('Escape');
	await pick('Relocate');
	await expect(page.getByTestId('track-relocate-menu')).toBeVisible({ timeout: 15_000 });
});
