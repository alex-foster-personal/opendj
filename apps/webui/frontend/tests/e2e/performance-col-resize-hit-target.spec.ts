import { expect, test } from '@playwright/test';

// Regression for the TrackTable `.col-resize` stacking-context bug: each
// `thead th` is `position: sticky`, which establishes its own stacking
// context, so a resize handle's `z-index: 2` only wins paint order WITHIN
// its own th - it cannot paint above a later-DOM-order sibling th, which
// always wins regardless of z-index. Before the fix, every ON-SCREEN
// handle's own center point resolved to the NEXT column's th, not the
// handle itself (mirrors the probe in performance-feedback-card-dismiss.spec.ts,
// which hit exactly this and had to search for an "unobstructed" handle
// rather than trusting any single one - that spec's own locator logic is
// the reason this fix matters, not just a synthetic check here).
test('a real column resize handle is hittable at its own center point', async ({ page }) => {
	await page.goto('/performance', { waitUntil: 'domcontentloaded' });
	await expect(page.locator('[data-testid="track-table"]')).toBeVisible({ timeout: 60_000 });

	const result = await page.locator('.col-resize').evaluateAll((handles) => {
		const samples: Array<{ hit: boolean; resolvedTag: string | null }> = [];
		for (const handle of handles) {
			const rect = handle.getBoundingClientRect();
			// Only consider handles actually on screen - a column scrolled
			// past the viewport's right edge has no paint at its point at
			// all (elementFromPoint returns null off-viewport), which is a
			// scroll-clipping fact, not the stacking-context bug this test
			// targets.
			const onScreen =
				rect.width > 0 &&
				rect.height > 0 &&
				rect.x >= 0 &&
				rect.y >= 0 &&
				rect.x < window.innerWidth &&
				rect.y < window.innerHeight;
			if (!onScreen) continue;
			const x = rect.x + rect.width / 2;
			const y = rect.y + rect.height / 2;
			const resolved = document.elementFromPoint(x, y);
			samples.push({ hit: handle.contains(resolved), resolvedTag: resolved?.tagName ?? null });
		}
		return samples;
	});

	// At least one on-screen handle must exist to make this assertion
	// meaningful.
	expect(result.length).toBeGreaterThan(0);
	// At least one real handle's own center must resolve to itself - not
	// the next column's th (or anything else painted over it). Mirrors the
	// "find an unobstructed handle" methodology in
	// performance-feedback-card-dismiss.spec.ts exactly.
	const hit = result.some((sample) => sample.hit);
	expect(hit, `no unobstructed handle among: ${JSON.stringify(result)}`).toBe(true);
});
