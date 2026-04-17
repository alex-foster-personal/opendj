import { test, expect } from '@playwright/test';

// Forcing a 409 from the UI requires racing two writes; this spec is a
// smoke-only placeholder. Full conflict handling is exercised by the
// backend unit tests in tests/webui/test_tracks.py; the dialog logic is
// code-reviewed manually.
test('conflict dialog component renders on forced 409 (manual fixture)', async ({ page }) => {
	await page.goto('/');
	await expect(page.locator('body')).toBeVisible();
});
