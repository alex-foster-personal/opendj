/**
 * requirement: LIBTAG-7898b
 *
 * The real app against a real engine (Library Organization PRD R-TAG, R-UNDO-1):
 * - if adding a tag from the Tags cell does not show it as #name on the row then broken
 * - if a new name becomes a tag without the one-click confirm then broken
 * - if removing a tag has no confirm, or Cmd+Z does not bring it back, then broken
 * - if Cmd+Z in the tag editor's text box undoes a library change then broken
 */
import { expect, test, type Page } from '@playwright/test';

async function openFixturePlaylist(page: Page): Promise<void> {
	await page.addInitScript(() => sessionStorage.setItem('odj.enrich-card.hidden', '1'));
	await page.goto('/performance?muted=1');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await page.getByTestId('playlist-row').filter({ hasText: 'E2E Fixture Set' }).click();
	await expect(page.getByTestId('tag-cell').first()).toBeAttached();
}

test('add a new tag, remove it with a confirm, and bring it back with undo', async ({ page }) => {
	const errors: string[] = [];
	page.on('pageerror', (error) => errors.push(error.message));
	// One Cmd+Z, one stack: the playlist undo must not fire beside the tag undo.
	const nothingToUndo: string[] = [];
	page.on('request', (request) => {
		if (request.url().includes('/playlist-history/undo')) nothingToUndo.push(request.url());
	});
	await page.setViewportSize({ width: 1600, height: 900 });
	await openFixturePlaylist(page);

	const name = `e2etag${Date.now()}`;
	const cell = page.getByTestId('tag-cell').first();
	await cell.hover();
	await cell.getByTestId('tag-add').click();
	const editor = page.getByTestId('tag-editor');
	await editor.getByRole('textbox').fill(name);
	await editor.getByTestId('tag-create').click();
	await expect(editor.getByTestId('tag-create-confirm')).toContainText(`#${name}`);
	await expect(cell).not.toContainText(name);
	await editor.getByTestId('tag-create-yes').click();
	await expect(cell).toContainText(`#${name}`);

	await cell.getByText(`#${name}`).hover();
	await cell.getByRole('button', { name: `Remove #${name}` }).click();
	await page.getByTestId('tag-remove-yes').click();
	await expect(cell).not.toContainText(name);

	await page.locator('body').click({ position: { x: 5, y: 5 } });
	await page.keyboard.press('Meta+z');
	await expect(cell).toContainText(`#${name}`);
	await expect(page.getByText('Undid tag change')).toBeVisible();
	await page.waitForTimeout(500);
	expect(nothingToUndo).toEqual([]);

	await cell.hover();
	await cell.getByTestId('tag-add').click();
	await editor.getByRole('textbox').focus();
	await page.keyboard.press('Meta+z');
	await page.keyboard.press('Escape');
	await expect(cell).toContainText(`#${name}`);
	expect(errors).toEqual([]);
});
