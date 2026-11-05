import { expect, test } from '@playwright/test';

// requirement: LIBUX-34, STANDALONE-03
// [if] All Tracks lists one fixture row with an own beatgrid record [then] the BPM
// hover shows Beat grid provenance [else stop]. Other rows stay missing controls.

const MISSING_BPM_TITLE =
	'no own analysis record for this lane yet Double-click, or right-click and choose Edit, to change it.';

type BpmRowSnapshot = {
	stableId: string;
	title: string;
	text: string;
};

async function snapshotBpmRows(page: import('@playwright/test').Page): Promise<BpmRowSnapshot[]> {
	const rows = page.locator('[data-testid="track-row"]');
	await expect(rows.first()).toBeVisible({ timeout: 30_000 });
	return rows.evaluateAll((rowEls) =>
		rowEls.map((row) => {
			const stableId = row.getAttribute('data-stable-id') ?? '';
			const cell = row.querySelector('td.c-bpm');
			return {
				stableId,
				title: cell?.getAttribute('title') ?? '',
				text: (cell?.textContent ?? '').trim()
			};
		})
	);
}

test('All Tracks BPM hover shows beatgrid provenance on the seeded row', async ({ page }) => {
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await page.getByText('All Tracks', { exact: true }).first().click();

	const snapshots = await snapshotBpmRows(page);
	expect(snapshots.length).toBeGreaterThan(0);

	const gridRows = snapshots.filter((row) => row.title.includes('Beat grid: own beatgrid.backfill.'));
	expect(gridRows).toHaveLength(1);
	const gridRow = gridRows[0]!;
	expect(gridRow.title).toContain('Confidence: 95%.');
	expect(gridRow.text).toBe('121.9');

	for (const row of snapshots) {
		if (row.stableId === gridRow.stableId) continue;
		expect(row.title).toBe(MISSING_BPM_TITLE);
		expect(row.text).toBe('');
	}
});
