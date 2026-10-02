import { expect, test } from '@playwright/test';

// requirement: LIBUX-31
// [if] All Tracks lists a row whose BPM has beatgrid provenance [then] its BPM
// hover names the method, exactly as a playlist row does [else stop].
// Mac check on PR #4014 (Fri 2 Oct 2026): GET /api/v1/tracks dropped
// bpm_source/bpm_method/bpm_confidence, so All Tracks and its search read only
// "Exact BPM" while the playlist view named the beat grid.

test('All Tracks BPM hover names the beat grid method', async ({ page }) => {
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await page.getByText('All Tracks', { exact: true }).first().click();
	const cells = page.locator('[data-testid="track-row"] td.c-bpm');
	await expect(cells.first()).toBeVisible({ timeout: 30_000 });
	// The fixture's tracks all carry provenance on the shared row builder.
	const titles = await cells.evaluateAll((els) => els.map((el) => el.getAttribute('title') ?? ''));
	expect(titles.length).toBeGreaterThan(0);
	for (const title of titles) expect(title).toMatch(/^Beat grid: \S/);
});
