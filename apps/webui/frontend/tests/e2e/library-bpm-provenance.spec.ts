import { expect, test } from '@playwright/test';

// requirement: LIBUX-34, STANDALONE-03
// [if] All Tracks lists a fixture row with no beatgrid [then] the BPM hover
// names why the cell is empty [else stop].
// The root fixture (deckload_fixture.build) writes no bpm and no beatgrid.
// Serving bpm_status=missing used to be hidden, so the hover led with
// "Beat grid: own-analysis" from the placeholder source. The empty cell now
// says why. A row that really has a method still leads with "Beat grid:"
// (bpm-cell-title unit).

test('All Tracks BPM hover names why an empty cell is empty', async ({ page }) => {
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await page.getByText('All Tracks', { exact: true }).first().click();
	const cells = page.locator('[data-testid="track-row"] td.c-bpm');
	await expect(cells.first()).toBeVisible({ timeout: 30_000 });
	const titles = await cells.evaluateAll((els) => els.map((el) => el.getAttribute('title') ?? ''));
	expect(titles.length).toBeGreaterThan(0);
	// LIBPG-7694: the cell is now editable, so its hover adds the edit hint after the reason.
	for (const title of titles) {
		expect(title).toBe(
			'no own analysis record for this lane yet Double-click, or right-click and choose Edit, to change it.'
		);
	}
	const labels = await cells.evaluateAll((els) => els.map((el) => (el.textContent ?? '').trim()));
	for (const label of labels) expect(label).toBe('missing');
});
