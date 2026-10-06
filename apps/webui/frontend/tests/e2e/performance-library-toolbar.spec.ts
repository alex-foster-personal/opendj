/**
 * requirement: LIBUX-49
 *
 * the maintainer, Tue 6 Oct 2026, on the library toolbar:
 * - B7: the tag-edit buttons (Find & Replace, Bulk Edit, MyTags) wrapped the
 *   one-line bar onto several lines. They now sit behind ONE pencil whose menu
 *   opens on click.
 * - B8: hide the second search bar (the add-track search) for now.
 *
 * Hidden is not removed: the add-track search stays mounted on an editable
 * playlist, and the routes behind it keep answering while it is hidden.
 *
 * Regression lines:
 * - if any library header control sits on a second line at 1280x800 on an editable playlist then broken
 * - if Find & Replace, Bulk Edit or MyTags show before the pencil is clicked, or not after, then broken
 * - if the pencil's hover tip still draws over the open menu then broken
 * - if a menu item does not open its own editor, or the selection items work with nothing selected, then broken
 * - if the add-track search is visible, or unmounted, on an editable playlist then broken
 * - if GET /tracks?q= or PUT /playlists/{id}/tracks stops answering while the search is hidden then broken
 */
import { expect, test, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { FIXTURE_MANIFEST_PATH } from './playwright.performance.config';

type Manifest = { populated_playlist_id: string; tracks: { stable_id: string; title?: string | null }[] };

function manifest(): Manifest {
	return JSON.parse(readFileSync(FIXTURE_MANIFEST_PATH, 'utf8')) as Manifest;
}

async function openFixturePlaylist(page: Page): Promise<void> {
	// The first-run enrichment card floats over the bottom right of the library.
	await page.addInitScript(() => sessionStorage.setItem('odj.enrich-card.hidden', '1'));
	await page.goto('/performance?muted=1');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await page.getByTestId('playlist-row').filter({ hasText: 'E2E Fixture Set' }).click();
	// The add-track search mounts only for an editable pane (settled etag).
	await expect(page.getByTestId('add-track-parked')).toBeAttached();
}

test('the edit actions sit behind one pencil and the header stays one line', async ({ page }) => {
	await page.setViewportSize({ width: 1280, height: 800 });
	await openFixturePlaylist(page);

	// One line: every visible toolbar control overlaps the same horizontal band (the
	// pane tabs on the left stack their own stepper on purpose, so they are not judged). A
	// pixel cap would depend on the platform's font metrics; a second row would not.
	const rows = await page.locator('.pane-header .header-right').evaluate((header) => {
		const boxes = [...header.querySelectorAll('button, label, select')]
			.map((el) => el.getBoundingClientRect())
			.filter((r) => r.width > 0 && r.height > 0);
		return { lowestTop: Math.max(...boxes.map((r) => r.top)), highestBottom: Math.min(...boxes.map((r) => r.bottom)) };
	});
	expect(rows.lowestTop, 'the library header wrapped onto a second line').toBeLessThan(rows.highestBottom);

	const pencil = page.getByTestId('library-edit-menu');
	const items = ['Find & Replace', 'Bulk Edit', 'MyTags'].map((name) =>
		page.getByRole('menuitem', { name, exact: true })
	);
	await expect(pencil).toHaveAttribute('aria-label', 'Edit tags');
	for (const item of items) await expect(item).toBeHidden();

	await pencil.click();
	for (const item of items) await expect(item).toBeVisible();
	// The pointer is still on the pencil: its hover tip must not draw over the open menu.
	// (Hidden, or kept only as the screen-reader copy that aria-describedby points at.)
	const tipDrawn = await page.evaluate(() => {
		const tip = document.getElementById('single-hover-tip');
		return tip !== null && !tip.hidden && !tip.classList.contains('single-hover-tip-sr');
	});
	expect(tipDrawn, 'the pencil hover tip draws over its own open menu').toBe(false);
	// Nothing is selected yet: the two selection actions say why they are off.
	await expect(items[0]).toBeDisabled();
	await expect(items[1]).toBeDisabled();
	await expect(items[0]).toHaveAttribute('title', 'Select one or more tracks first');
	await expect(items[2]).toBeEnabled();

	await items[2].click();
	await expect(page.getByRole('dialog', { name: 'MyTags' })).toBeVisible();
	await page.getByRole('dialog', { name: 'MyTags' }).getByRole('button', { name: 'close' }).click();
	for (const item of items) await expect(item).toBeHidden();

	await page.locator('[data-testid="track-row"]').first().click();
	await pencil.click();
	await expect(items[0]).toBeEnabled();
	await items[0].click();
	await expect(page.getByRole('dialog', { name: /^Find & Replace - 1 track/ })).toBeVisible();
	await page.getByRole('dialog', { name: /^Find & Replace/ }).getByRole('button', { name: 'close' }).click();

	await pencil.click();
	await items[1].click();
	await expect(page.getByRole('dialog', { name: /^Bulk Edit - 1 track/ })).toBeVisible();
	await page.getByRole('dialog', { name: /^Bulk Edit/ }).getByRole('button', { name: 'close' }).click();

	await pencil.click();
	await page.keyboard.press('Escape');
	await expect(pencil).toHaveAttribute('aria-expanded', 'false');
});

test('the add-track search is hidden, still mounted, and its routes keep answering', async ({ page }) => {
	const { populated_playlist_id: playlistId, tracks } = manifest();
	await page.setViewportSize({ width: 1280, height: 800 });
	await openFixturePlaylist(page);
	await expect(page.getByTestId('add-track-parked')).toBeHidden();
	await expect(page.getByPlaceholder('Add track (title/artist)')).toHaveCount(1);
	await expect(page.getByPlaceholder('Add track (title/artist)')).toBeHidden();

	// The search the control ran: GET /tracks?q= (title/artist substring).
	const probe = tracks.find((t) => typeof t.title === 'string' && t.title.trim().length >= 2);
	const q = probe?.title?.trim().slice(0, 4) ?? tracks[0].stable_id.slice(0, 4);
	const found = await page.request.get(`/api/v1/tracks?q=${encodeURIComponent(q)}&limit=8`);
	expect(found.status(), await found.text()).toBe(200);
	expect(((await found.json()) as { items: unknown[] }).items.length).toBeGreaterThan(0);

	// The write it made: PUT /playlists/{id}/tracks with the membership etag. The
	// same membership back is a real write that changes nothing.
	const detail = await page.request.get(`/api/v1/playlists/${playlistId}`);
	expect(detail.status()).toBe(200);
	const etag = detail.headers()['etag'];
	expect(etag, 'playlist detail must carry its membership ETag').toBeTruthy();
	const items = ((await detail.json()) as { items: string[] }).items;
	const put = await page.request.put(`/api/v1/playlists/${playlistId}/tracks`, {
		headers: { 'If-Match': etag },
		data: { stable_ids: items }
	});
	expect(put.status(), await put.text()).toBe(200);
});
