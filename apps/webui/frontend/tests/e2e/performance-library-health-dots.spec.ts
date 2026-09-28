/**
 * pin be1b8f94f167 (reopened): the browser-sources health dots must be
 * horizontal, bottom-right (done in #1051), and cover FOUR coverage signals -
 * library health, vocals completion, stems completion, and lyrics
 * completion.
 *
 * pin a66ee132a14e: e03dc164d ("move the status dots to the bottom right")
 * carried only the library signal across into this row and silently dropped
 * the other two dots the old `conn-dots` strip had (Frontend online, Backend
 * online) - the only signals that distinguish "the engine is not answering"
 * from "the library really is empty". They are restored here as two more
 * dots in the SAME cluster, so the row is now SIX dots, not four.
 *
 * #1051 shipped library/vocals/stems but the reopen found vocal detection
 * is not proof lyric analysis ran: this proves a real, distinct lyrics dot
 * exists (backed by GET /ingest/coverage's "lyrics" key, itself backed by
 * the real per-track apps/lyrics cache, not vocals).
 *
 * issue #2076 supersedes the per-dot distinct-popover hover contract from
 * pin be1b8f94f167: hovering any dot reveals one shared popover listing all
 * six checks, and the cluster lives inside the bottom tray (.bottom-bar).
 *
 * WHAT THIS EXISTS TO CATCH. A unit test can fake IngestCoverage and never
 * notice the dot never actually renders, or that the dots collapsed back to
 * four because the restored liveness dots got dropped again. It must also
 * fail if the coverage REQUEST itself is broken (a missing key, a 500, a
 * frontend/backend contract mismatch) rather than accepting six "error" dots
 * as if that were a passing layout check.
 *
 * Acceptance:
 *   [if] fewer than 6 .health-dot buttons render          [then STOP] a
 *        signal was dropped or never added
 *   [if] the /ingest/coverage request fails, or any dot lands in its
 *        'error' state                                    [then STOP] the
 *        coverage contract is broken, not merely slow
 *   [if] a dot's aria-label is missing or generic          [then STOP] the
 *        per-dot label is not real
 *   [if] hovering any dot does not reveal the shared popover with all six
 *        labels                                           [then STOP] the
 *        unified health hover requirement is unmet
 *   [if] any dot is not inside .bottom-bar                 [then STOP] the
 *        cluster regressed to floating above the NEXT row
 *   [if] the dots are not laid out left-to-right (increasing x), in the
 *        panel's lower half                               [then STOP] the
 *        horizontal bottom layout regressed
 */
import { expect, test } from '@playwright/test';

const EXPECTED_LABELS = [
	'Frontend',
	'Backend',
	'Library health',
	'Vocals completion',
	'Stems completion',
	'Lyrics completion'
];

test('pin be1b8f94f167: browser-sources health dots cover library/vocals/stems/lyrics with shared hover detail in the bottom tray', async ({
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
		/playable complete, \d+ missing, \d+ broken (link|links)/
	);

	const ariaLabels = dotStates.map((s) => s.label);
	expect(ariaLabels).toHaveLength(EXPECTED_LABELS.length);
	for (const expectedLabel of EXPECTED_LABELS) {
		expect(ariaLabels.some((label) => label.startsWith(`${expectedLabel}:`))).toBe(true);
	}

	// Every dot must live inside the bottom tray, not float above NEXT/RECC.
	for (let i = 0; i < EXPECTED_LABELS.length; i++) {
		expect(
			await dots.nth(i).evaluate((el) => el.closest('.bottom-bar') !== null)
		).toBe(true);
	}

	// Horizontal, left-to-right layout.
	const boxes = await dots.evaluateAll((elements) =>
		elements.map((el) => el.getBoundingClientRect().x)
	);
	for (let i = 1; i < boxes.length; i++) {
		expect(boxes[i]).toBeGreaterThan(boxes[i - 1]);
	}

	// Lower half of the browser panel.
	const panelBox = await page.locator('.rb-browser').evaluate((el) => el.getBoundingClientRect());
	const dotBoxes = await dots.evaluateAll((elements) =>
		elements.map((el) => {
			const r = el.getBoundingClientRect();
			return { top: r.top };
		})
	);
	for (const box of dotBoxes) {
		expect(box.top).toBeGreaterThan(panelBox.top + (panelBox.bottom - panelBox.top) / 2);
	}

	// Hover any dot reveals the one shared popover with all six labels.
	const sharedPopover = page.locator('.library-health .health-popover');

	await dots.nth(0).hover();
	await expect(sharedPopover).toBeVisible();
	const firstHoverText = await sharedPopover.textContent();
	for (const expectedLabel of EXPECTED_LABELS) {
		expect(firstHoverText).toContain(expectedLabel);
	}

	await page.mouse.move(0, 0);
	await expect(sharedPopover).toBeHidden();

	await dots.nth(EXPECTED_LABELS.length - 1).hover();
	await expect(sharedPopover).toBeVisible();
	const lastHoverText = await sharedPopover.textContent();
	for (const expectedLabel of EXPECTED_LABELS) {
		expect(lastHoverText).toContain(expectedLabel);
	}

	await page.mouse.move(0, 0);
	await expect(sharedPopover).toBeHidden();
});
