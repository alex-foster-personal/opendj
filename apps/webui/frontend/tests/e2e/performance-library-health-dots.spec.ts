/**
 * pin be1b8f94f167 (reopened): the browser-sources health dots must be
 * horizontal, bottom-right (done in #1051), and cover FOUR signals -
 * library health, vocals completion, stems completion, and lyrics
 * completion - each hoverable to its own full label+detail popover.
 *
 * #1051 shipped library/vocals/stems but the reopen found vocal detection
 * is not proof lyric analysis ran: this proves a real, distinct lyrics dot
 * exists (backed by GET /ingest/coverage's "lyrics" key, itself backed by
 * the real per-track apps/lyrics cache, not vocals) and that every dot's
 * hover popover repeats its own label, not a shared/generic one.
 *
 * WHAT THIS EXISTS TO CATCH. A unit test can fake IngestCoverage and never
 * notice the dot never actually renders, that two dots share one popover
 * (so hovering one leaks another's detail), or that the dots collapsed
 * back to three because "vocals" and "lyrics" got merged again. It must
 * also fail if the coverage REQUEST itself is broken (a missing key, a
 * 500, a frontend/backend contract mismatch) rather than accepting four
 * "error" dots as if that were a passing layout check - and it must fail
 * if the group is anchored to the bottom-LEFT instead of the right, since
 * checking only "lower half" cannot tell those apart.
 *
 * Acceptance:
 *   [if] fewer than 4 .health-dot buttons render          [then STOP] a
 *        signal was dropped or never added
 *   [if] the /ingest/coverage request fails, or any dot lands in its
 *        'error' state                                    [then STOP] the
 *        coverage contract is broken, not merely slow
 *   [if] a dot's aria-label is missing or generic          [then STOP] the
 *        per-dot label is not real
 *   [if] hovering a dot does not reveal ITS OWN popover text
 *        (label + detail) distinct from the other dots     [then STOP] the
 *        "repeated w labels" hover requirement is unmet
 *   [if] the dots are not laid out left-to-right (increasing x), in the
 *        panel's lower half, AND within tolerance of the panel's RIGHT
 *        edge                                              [then STOP] the
 *        horizontal bottom-right layout regressed (lower-half alone
 *        cannot distinguish bottom-right from bottom-left)
 */
import { expect, test } from '@playwright/test';

const EXPECTED_LABELS = [
	'Library health',
	'Vocals completion',
	'Stems completion',
	'Lyrics completion'
];

// How close the last dot's right edge must sit to the panel's right edge.
// Generous enough to tolerate the dot's own small hit target and the
// container's padding, tight enough that "moved to the opposite side"
// cannot pass.
const RIGHT_EDGE_TOLERANCE_PX = 60;

test('browser-sources health dots cover library/vocals/stems/lyrics with per-dot hover detail', async ({
	page
}) => {
	const coverageResponse = page.waitForResponse((response) =>
		response.url().includes('/api/v1/ingest/coverage')
	);

	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	// Anchor: the dots live beside the browser-sources nav rail.
	await expect(page.locator('nav[aria-label="browser sources"]')).toBeVisible();

	const dots = page.locator('.library-health .health-dot');
	await expect(dots).toHaveCount(EXPECTED_LABELS.length);

	// The coverage request itself must succeed - a broken endpoint or a
	// frontend/backend contract mismatch must fail this test, not merely
	// produce four quietly-accepted error dots.
	const response = await coverageResponse;
	expect(response.ok(), `GET /ingest/coverage returned ${response.status()}`).toBe(true);
	const coverageBody = await response.json();
	expect(coverageBody.missing, 'coverage response missing the "missing" map').toBeTruthy();
	expect(
		typeof coverageBody.missing.lyrics,
		'coverage response has no "lyrics" key - the signal this pin adds'
	).toBe('number');

	// Wait for the async coverage load to settle AND land in a REAL,
	// non-error state - initial state is "checking ... coverage" for every
	// dot; accepting 'error' here would let a broken response pass as long
	// as the UI merely stopped saying "checking".
	await expect
		.poll(async () => {
			const classNames = await dots.evaluateAll((elements) => elements.map((el) => el.className));
			return classNames.every((cls) => /\b(complete|incomplete|unavailable)\b/.test(cls));
		})
		.toBe(true);

	const dotStates = await dots.evaluateAll((elements) =>
		elements.map((el) => ({
			label: el.getAttribute('aria-label') ?? '',
			isError: el.classList.contains('error')
		}))
	);
	for (const state of dotStates) {
		expect(state.isError, `${state.label} landed in the 'error' state`).toBe(false);
	}
	// The lyrics dot specifically must reach a real measured verdict
	// (complete/incomplete), the exact thing this pin adds - not merely
	// "not error", which an 'unavailable' fallback could also satisfy.
	const lyricsLabel = dotStates.find((s) => s.label.startsWith('Lyrics completion:'))?.label ?? '';
	expect(lyricsLabel, 'Lyrics completion dot never reached a real coverage verdict').toMatch(
		/complete, \d+ missing, \d+ unreachable/
	);

	const ariaLabels = dotStates.map((s) => s.label);
	expect(ariaLabels).toHaveLength(EXPECTED_LABELS.length);
	for (const expectedLabel of EXPECTED_LABELS) {
		expect(ariaLabels.some((label) => label.startsWith(`${expectedLabel}:`))).toBe(true);
	}

	// Horizontal, left-to-right layout.
	const boxes = await dots.evaluateAll((elements) =>
		elements.map((el) => el.getBoundingClientRect().x)
	);
	for (let i = 1; i < boxes.length; i++) {
		expect(boxes[i]).toBeGreaterThan(boxes[i - 1]);
	}

	// Bottom-RIGHT of the browser panel: every dot in the lower half AND the
	// last dot's right edge within tolerance of the panel's right edge -
	// checking only "lower half" would also pass a bottom-LEFT layout.
	const panelBox = await page.locator('.rb-browser').evaluate((el) => el.getBoundingClientRect());
	const dotBoxes = await dots.evaluateAll((elements) =>
		elements.map((el) => {
			const r = el.getBoundingClientRect();
			return { top: r.top, right: r.right };
		})
	);
	for (const box of dotBoxes) {
		expect(box.top).toBeGreaterThan(panelBox.top + (panelBox.bottom - panelBox.top) / 2);
	}
	const lastDotRight = dotBoxes[dotBoxes.length - 1].right;
	expect(panelBox.right - lastDotRight).toBeGreaterThanOrEqual(0);
	expect(panelBox.right - lastDotRight).toBeLessThanOrEqual(RIGHT_EDGE_TOLERANCE_PX);

	// Hover reveals a DISTINCT popover per dot - label repeated, not shared.
	for (let i = 0; i < EXPECTED_LABELS.length; i++) {
		const dot = dots.nth(i);
		await dot.hover();
		const popover = dot.locator('.health-popover');
		await expect(popover).toBeVisible();
		await expect(popover.locator('strong')).toHaveText(EXPECTED_LABELS[i]);
		const popoverText = await popover.textContent();
		expect(popoverText).toContain(EXPECTED_LABELS[i]);
		// Moving off hides it again, proving each is its own toggle rather
		// than one popover that got repositioned.
		await page.mouse.move(0, 0);
		await expect(popover).toBeHidden();
	}
});
