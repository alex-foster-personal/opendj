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

	// The table enters the DOM before the app has painted a frame: the
	// 2026-10-16 production run (source 69e55fe7a) measured 12 handles whose
	// rects all passed the on-screen filter while `document.elementFromPoint`
	// returned null at every centre, and its failure screenshot still showed
	// the boot screen. Two animation frames after the visible check make the
	// samples read a painted frame rather than a laid-out one; they cannot
	// manufacture a hit, because the assertion below still requires a handle
	// to resolve to itself.
	//
	// The header row can also sit past the bottom edge at this project's
	// 1280x800 viewport: the 2026-10-19 run measured every handle rect at
	// y=792 with height 19.5 (centres at y=801.75, innerHeight 800), so the
	// sampled point was 1.75 px off-screen and `elementFromPoint` returned
	// null for a reason that has nothing to do with the stacking context.
	// Scroll the handle a user would actually grab into view first - a real
	// scroll, no injected event - and only then sample.
	await page.locator('.col-resize').first().scrollIntoViewIfNeeded();
	await page.evaluate(
		() =>
			new Promise<void>((resolve) => {
				requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
			})
	);

	const result = await page.locator('.col-resize').evaluateAll((handles) => {
		const samples: Array<{
			hit: boolean;
			resolvedTag: string | null;
			rect: { x: number; y: number; width: number; height: number };
			center: { x: number; y: number };
			centerInsideViewport: boolean;
		}> = [];
		const viewport = { width: window.innerWidth, height: window.innerHeight };
		for (const handle of handles) {
			const rect = handle.getBoundingClientRect();
			// Only consider handles whose CENTRE is on screen. `elementFromPoint`
			// resolves a painted point, so a centre outside the viewport returns
			// null for a scroll-clipping reason, not the stacking-context bug this
			// test targets - and the earlier filter tested the rect ORIGIN while
			// sampling the centre, which is how 12 off-viewport centres read as
			// 12 nulls.
			const x = rect.x + rect.width / 2;
			const y = rect.y + rect.height / 2;
			const centerInsideViewport =
				rect.width > 0 &&
				rect.height > 0 &&
				x >= 0 &&
				y >= 0 &&
				x < viewport.width &&
				y < viewport.height;
			if (!centerInsideViewport) continue;
			const resolved = document.elementFromPoint(x, y);
			// Record the geometry, not just the verdict: with only `hit` and
			// `resolvedTag` a null result cannot be told apart from an
			// off-viewport centre (rect origin inside the viewport, centre
			// outside it) or from a real paint obstruction.
			samples.push({
				hit: handle.contains(resolved),
				resolvedTag: resolved?.tagName ?? null,
				rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
				center: { x, y },
				centerInsideViewport:
					x >= 0 && y >= 0 && x < viewport.width && y < viewport.height
			});
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
