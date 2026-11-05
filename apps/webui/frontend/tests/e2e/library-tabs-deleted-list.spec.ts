/**
 * LIBUX-8677b: a saved library tab naming a list deleted since is dropped on
 * restore with a warn toast (news, not a failure), and the cleaned tabs are
 * saved so the next boot says nothing.
 *
 * Regression lines:
 * - if a boot with a deleted list in the saved tabs logs a console error then broken
 * - if that boot does not tell the user the tab was dropped then broken
 * - if the next boot still names the deleted list then broken
 */
import { expect, test } from '@playwright/test';

const TREE = '[data-testid="playlist-tree"]';
const NOTICE = /saved library tab\(s\) name a list that no longer exists/;
const GONE = 'f'.repeat(32);

test('a saved tab on a deleted list warns once and is saved away', async ({ page }) => {
	const before = (await (await page.request.get('/api/v1/ui-prefs')).json()).library_tabs;
	const tab = { ...(before.tabs[0] ?? {}), playlist_id: GONE, name: 'Deleted since', search: '' };
	const put = await page.request.put('/api/v1/ui-prefs', {
		data: { library_tabs: { ...before, tabs: [...before.tabs, tab] } }
	});
	expect(put.ok(), `PUT ui-prefs: ${put.status()} ${await put.text()}`).toBeTruthy();

	const errors: string[] = [];
	page.on('console', (msg) => {
		if (msg.type() === 'error' && NOTICE.test(msg.text())) errors.push(msg.text());
	});
	await page.goto('/performance');
	await page.waitForSelector(TREE, { timeout: 60_000 });
	await expect(page.getByText(NOTICE).first()).toBeVisible({ timeout: 30_000 });
	expect(errors, 'a dropped tab is a notice, not a console error').toEqual([]);
	await expect
		.poll(async () => JSON.stringify((await (await page.request.get('/api/v1/ui-prefs')).json()).library_tabs), {
			timeout: 10_000
		})
		.not.toContain(GONE);
});
