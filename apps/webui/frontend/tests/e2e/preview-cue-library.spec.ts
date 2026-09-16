/**
 * CUEOUT-15: previewing a library track on the cue bus never moves a deck.
 *
 * The click used to seek EVERY deck holding that `stable_id`, with no check on
 * `playing` and none on `is_master`, and refused outright when no deck held it
 * (`preview seek: track not on a deck`). Both halves are regressions this spec
 * pins, and the deck half is the dangerous one: a click while browsing could
 * jump a deck that was live on air.
 *
 * It drives the AGENT path (`preview_cue` / `preview_stop`) rather than a
 * pointer on the mini-waveform, for a fixture reason worth stating: this
 * suite's generated library has no ANLZ waveform data, so `PreviewStrip`
 * renders its `-` placeholder and there is no strip to click. That is not a
 * weaker test of the thing that matters. The strip's own click handler resolves
 * a ratio and calls straight through to the same `previewCueSeek` this
 * dispatches, and agent parity means the two paths must agree anyway.
 * `tests/unit/preview-cue-policy.test.mjs` pins the refusal policy underneath
 * both.
 *
 * Acceptance:
 * - [if] a track is previewed [then ⛔️] the preview state names that track and
 *   reports it playing.
 * - [if] the previewed track is ALSO loaded on a deck [then ⛔️] that deck's
 *   position does not jump to the previewed point.
 * - [if] a second track is previewed [then ⛔️] the preview moves to it rather
 *   than a second preview existing.
 * - [if] the preview is stopped [then ⛔️] the preview state empties.
 */
import { expect, test, type Page } from '@playwright/test';

import type { PerformanceCommand, PerformanceState } from '../../src/lib/rb/performance-ipc.svelte';

const TRACK_ROW = '[data-testid="track-row"]';

/** Far enough into the track that a deck seeking there would be unmistakable
 *  against a deck sitting near its start. */
const PREVIEW_RATIO = 0.6;

async function _query(page: Page): Promise<PerformanceState> {
	return page.evaluate(() => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.query();
	});
}

async function _dispatch(page: Page, command: PerformanceCommand): Promise<void> {
	await page.evaluate(async (cmd) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		await ipc.dispatch(cmd);
	}, command);
}

/** Open the page, wait for the IPC surface, and get a library on screen. */
async function _openPerformance(page: Page): Promise<void> {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	// Quiet for headed runs. This is the ROOM volume; the cue path keeps its own
	// gain, which must stay above zero or the preview is refused by design.
	await _dispatch(page, { type: 'master_volume', value: 0.1 });
	await expect.poll(async () => page.locator(TRACK_ROW).count()).toBeGreaterThan(1);
}

async function _stableIdOfRow(page: Page, index: number): Promise<string> {
	const id = await page.locator(TRACK_ROW).nth(index).getAttribute('data-stable-id');
	if (id === null) throw new Error(`row ${index} has no data-stable-id`);
	return id;
}

test('previewing a library track plays it without moving the deck that holds it', async ({
	page
}) => {
	await _openPerformance(page);

	// Put the previewed track on a deck, so "never moves a deck" is about a deck
	// that actually holds it. Anything else would pass trivially.
	const previewed = await _stableIdOfRow(page, 0);
	await _dispatch(page, { type: 'load', deck: 1, stable_id: previewed });
	await expect
		.poll(async () => (await _query(page)).decks[1].stable_id, { timeout: 45_000 })
		.toBe(previewed);
	await expect.poll(async () => (await _query(page)).decks[1].duration_ms).not.toBeNull();

	await _dispatch(page, { type: 'preview_cue', stable_id: previewed, ratio: PREVIEW_RATIO });
	await expect.poll(async () => (await _query(page)).preview.stable_id, { timeout: 30_000 }).toBe(
		previewed
	);

	const after = await _query(page);
	expect(after.preview.playing).toBe(true);
	expect(after.preview.duration_ms).not.toBeNull();
	// The preview landed where it was asked to, which is what makes the deck
	// assertion below meaningful: both are talking about the same point.
	expect(
		after.preview.position_ms / (after.preview.duration_ms as number),
		'the preview must start at the requested ratio'
	).toBeGreaterThan(PREVIEW_RATIO - 0.05);

	const deckSeekedTo = PREVIEW_RATIO * (after.decks[1].duration_ms as number);
	expect(
		Math.abs((after.decks[1].position_ms ?? 0) - deckSeekedTo),
		'the deck holding the previewed track must not have been seeked to it'
	).toBeGreaterThan(5_000);

	// A second preview MOVES the voice rather than adding one.
	const second = await _stableIdOfRow(page, 1);
	await _dispatch(page, { type: 'preview_cue', stable_id: second, ratio: 0.2 });
	await expect.poll(async () => (await _query(page)).preview.stable_id, { timeout: 30_000 }).toBe(
		second
	);

	await _dispatch(page, { type: 'preview_stop' });
	await expect.poll(async () => (await _query(page)).preview.stable_id).toBeNull();
	const stopped = await _query(page);
	expect(stopped.preview.playing).toBe(false);
	expect(stopped.preview.duration_ms).toBeNull();
	expect(stopped.preview.route).toBeNull();
	// Stopping a preview is not a transport action: the deck is untouched.
	expect(stopped.decks[1].stable_id).toBe(previewed);
});

/**
 * CUEOUT-15 R3. The preview holds decoded PCM, which is the most expensive
 * thing a library click can allocate (measured at about 0.34 MB per second of
 * audio, so 59 to 129 MB for a real track). This asserts the three properties
 * that make that safe, through `PerformanceState.preview` so an agent can read
 * residency the same way.
 *
 * Regression lines:
 * - if a preview holds nothing after it starts then the reported residency is
 *   fiction and every other assertion here is worthless
 * - if previewing a second track leaves the first resident above the budget
 *   then the cache grows without bound
 * - if stopping does not release then one preview costs the session its RAM
 *   for as long as the page is open, which is the bug this replaced
 */
test('preview residency is reported, bounded and released', async ({ page }) => {
	await _openPerformance(page);
	const first = await _stableIdOfRow(page, 0);
	const second = await _stableIdOfRow(page, 1);

	const idle = await _query(page);
	expect(idle.preview.cache_tracks, 'nothing previewed yet, nothing resident').toBe(0);
	expect(idle.preview.cache_bytes).toBe(0);
	expect(
		idle.preview.cache_budget_bytes,
		'a budget of zero would make every assertion below pass vacuously'
	).toBeGreaterThan(0);

	await _dispatch(page, { type: 'preview_cue', stable_id: first, ratio: 0.3 });
	await expect
		.poll(async () => (await _query(page)).preview.stable_id, { timeout: 30_000 })
		.toBe(first);
	const playing = await _query(page);
	expect(playing.preview.cache_tracks, 'the decoded track must be reported').toBeGreaterThan(0);
	expect(playing.preview.cache_bytes, 'decoded PCM is never zero bytes').toBeGreaterThan(0);

	await _dispatch(page, { type: 'preview_cue', stable_id: second, ratio: 0.3 });
	await expect
		.poll(async () => (await _query(page)).preview.stable_id, { timeout: 30_000 })
		.toBe(second);
	const both = await _query(page);
	// Two tracks MAY both be resident now, which is the point of the cache, but
	// never past the budget.
	expect(
		both.preview.cache_bytes,
		'the cache must never exceed its own budget'
	).toBeLessThanOrEqual(both.preview.cache_budget_bytes);

	await _dispatch(page, { type: 'preview_stop' });
	await expect.poll(async () => (await _query(page)).preview.stable_id).toBeNull();
	const stopped = await _query(page);
	expect(stopped.preview.cache_tracks, 'stop must release every decoded track').toBe(0);
	expect(stopped.preview.cache_bytes).toBe(0);
});
