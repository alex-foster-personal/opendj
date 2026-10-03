import { expect, test } from '@playwright/test';

// LIBM-129 row in Settings, loaded on demand since PR #4014 (bundle budget):
// the row must still render inside the open overlay and still refuse a path
// that does not exist on disk, with the server's reason, before saving.

test('the watcher-folders settings row loads in the open overlay and validates paths', async ({
	page
}) => {
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await page.keyboard.press('ControlOrMeta+Comma');
	const search = page.locator('input.so-search');
	await expect(search).toBeVisible({ timeout: 15_000 });
	await search.fill('watcher');
	const textarea = page.getByRole('textbox', { name: /watcher/i });
	await expect(textarea).toBeVisible({ timeout: 15_000 });
	await textarea.fill('/definitely/not/a/real/folder-4014');
	await page.locator('.so-path-lines button', { hasText: 'Apply' }).click();
	await expect(page.locator('.so-path-lines-msg')).toContainText(/does not exist|not exist|missing/i);
});
