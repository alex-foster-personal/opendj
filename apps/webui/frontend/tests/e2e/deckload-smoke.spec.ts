/**
 * Cross-browser deck-load smoke: opening the library and loading a track
 * into deck 1 succeeds with no error toast, on BOTH chromium and webkit.
 * (#770 -- the WebKit-specific class of failure `smoke.spec.ts` and the
 * chromium-only root e2e config cannot see.)
 *
 * WHY A SEPARATE FILE FROM webkit-deckload.spec.ts: that suite measures the
 * real audio path (tempo, key, loop) against a real worklet, and two of its
 * tests are QUARANTINED off the PR-blocking gate for measured AudioContext
 * flakiness on a headless Linux runner (#637, #680 -- see e2e.yml). This
 * smoke asserts only the LOAD-SETTLE signal: `stable_id` landing and HOLDING
 * across consecutive polls, the same predicate `_waitForDeckLoaded` in that
 * suite uses (#774). Deliberately cheap: no worklet timing, no spectral
 * measurement, one worker, no retries.
 *
 * WHY THE LOAD IS A BUTTON CLICK, NOT A DOUBLE-CLICK: a double-click on a row
 * dispatches `onloadrow(row, deck, { play: true })`
 * (TrackTable.svelte:391-392) unconditionally once `dblclick_load_play` is
 * false -- there is no double-click path that loads without also playing.
 * `BrowserPanel.svelte:1479-1480` turns that `play: true` into a real `play`
 * performance command, which reaches `_resumeContext()`
 * (audio-engine.svelte.ts:3275) -- exactly the AudioContext code path
 * `#637`/`#680` quarantine as measured-flaky on headless Linux WebKit. A
 * PR-blocking smoke has no business anywhere near that path (caught on
 * review, PR #860). The per-row "Load onto deck N" button
 * (`TrackTable.svelte:1098`, `onloadrow(row, d)` with no `play` option) is
 * the load-ONLY interaction already documented as stable in
 * webkit-deckload.spec.ts's own header -- it is revealed by hover AND
 * row-selected together, hence the explicit title-cell
 * click (selects the row) then `row.hover()` below.
 *
 * WHY THIS RUNS AGAINST THE PRODUCTION ARTIFACT on BOTH browsers, sharing
 * playwright.webkit-deckload.config.ts's engine + fixture library, rather
 * than a second config against the vite dev server: the incident this smoke
 * exists for (#767) was a WebKit-only
 * failure mode -- `Response.json()` on a non-JSON body throws a different,
 * more cryptic message on WebKit than on Chromium -- observed in the
 * PACKAGED app, i.e. against the production transform. Running chromium
 * against the same artifact and engine, instead of a second dev-server
 * config, is what lets one spec assert the identical acceptance on both
 * browsers with no behavioural difference except the browser engine.
 *
 * THE #767 FAULT-INJECTION ACCEPTANCE CRITERION IS UNAVAILABLE, NOT COVERED
 * BY A SUBSTITUTE: `get_track_anlz`'s local (unmapped-track) branch only
 * reaches `local_anlz_payload` (local_waveform.py:519), whose single catch
 * of `LocalDecodeUnavailable` converts every ffmpeg failure mode this repo
 * has ever hit -- spawn OSError, decode timeout, nonzero exit, zero samples
 * -- into a 200 `not_decoded` response by design (local_waveform.py:239-256,
 * issue #735). `apps/webui/server/app.py` registers exception handlers for
 * exactly four typed errors; nothing else is caught, so the raw
 * `text/plain` shape this smoke originally injected via `page.route()` is
 * real and reachable for a truly unhandled exception, but this route's
 * current, hardened code has no such exception left to throw for this
 * fixture, and no other rb API call this deck-load path touches has one
 * either. Two attempts at substitute coverage were tried and rejected on
 * review (PR #860): a `page.route()` fulfill, and later a `globalThis.fetch`
 * stub in a unit test -- both are simulated responses that cannot detect a
 * regression in the real backend route, middleware, or cross-browser
 * `Response.json()` handling, which is what #767 actually broke. Per
 * AGENTS.md:L55-L59, an unreachable real path is reported UNAVAILABLE, not
 * fabricated as a passing gate test, so this smoke does that: the scenario
 * is not exercised here or anywhere else in this PR. `tests/unit/api-rb-
 * base.test.mjs` keeps a `globalThis.fetch`-stubbed test of
 * `_throwRbApiError`'s non-JSON-body handling (the same idiom that file
 * already uses for every other contract test in it) as a real regression
 * pin on that function's own behaviour -- it proves the helper still throws
 * rather than swallows, nothing more, and is not offered as #767 coverage.
 * Re-open the #767 fault-injection case only when a code path exists that
 * can genuinely raise an uncaught exception for a fixture in this suite.
 *
 * Requirements:
 *
 * - ✔︎ Both projects load a track into deck 1 via the load-only control,
 *   reach the settle predicate for THAT track's stable_id (load AND the
 *   deferred stems upgrade), and raise no toast at any point during the
 *   wait.
 * - ✔︎ The settle poll crosses the Playwright boundary with only the
 *   primitive deck-readiness fields it asserts, never the full live engine
 *   state while a load is pending.
 * - ✔︎ The load interaction never dispatches `play`, so this smoke cannot
 *   reach the quarantined AudioContext path (#637, #680).
 * - ✔︎ The real, non-intercepted /anlz call this fixture drives never itself
 *   returns the #767 shape, so the reasoning above is pinned against drift
 *   rather than merely asserted in a comment.
 * - ✔︎ The #767 fault-injection acceptance criterion is explicitly marked
 *   UNAVAILABLE with its reasoning, per AGENTS.md, rather than satisfied by
 *   a simulated response.
 * - ✔︎ PLAY-04 is mounted against the real engine and ingested fixture:
 *   changing the visible sort while AutoPlay is active exposes the frozen
 *   activation-order status, and an off/on cycle re-snapshots the new order.
 *
 * Acceptance tests:
 *
 * - [if] a track fails to reach `stable_id === <the clicked track's id>`
 *   with `stems.status` terminal [then ⛔️] the load smoke passes.
 * - [if] any toast is present at any point during the load or the deferred
 *   stems work after it [then ⛔️] the load smoke passes.
 * - [if] the settle poll requires unrelated live engine state to cross the
 *   Playwright boundary [then ⛔️] the WebKit load smoke passes.
 * - [if] the real /anlz route ever answers with a non-200 or a content-type
 *   other than JSON for this fixture [then ⛔️] the real-path contract test
 *   passes.
 * - [if] active AutoPlay silently follows a changed browser sort, or its
 *   activation-order notice survives an off/on re-snapshot [then ⛔️] the
 *   PLAY-04 browser acceptance test passes.
 */
import { expect, test, type Page } from '@playwright/test';

import type { DeckId } from '../../src/lib/rb/deck-id';

const PREFS_STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const TRACK_ROW = '[data-testid="track-row"]';
const AUTOPLAY_SNAPSHOT_NOTICE =
	'AutoPlay is using its activation order. Toggle it off and on to use this order.';

/** Same settle shape as webkit-deckload.spec.ts's DECK_SETTLE_* (#774). */
const DECK_SETTLE_POLLS = 3;
const DECK_SETTLE_POLL_MS = 100;
const DECK_SETTLE_TIMEOUT_MS = 45_000;

async function _waitForIpc(page: Page): Promise<void> {
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
		timeout: 30_000
	});
}

type DeckReadySample = {
	stableId: string | null;
	transportPending: boolean;
	stemsStatus: string;
	toastSeen: boolean;
};

async function _queryDeckReady(page: Page, deck: DeckId): Promise<DeckReadySample> {
	return page.evaluate((deckId) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		const state = ipc.query().decks[deckId];
		if (state === undefined) throw new Error(`deck ${deckId} is not installed`);
		return {
			stableId: state.stable_id,
			transportPending: state.transport_pending,
			stemsStatus: state.stems.status,
			toastSeen: document.querySelector('.toast-stack [data-toast-id]') !== null
		};
	}, deck);
}

async function _openAllTracks(page: Page): Promise<void> {
	await page.getByText('All Tracks', { exact: true }).first().click();
	await expect(page.locator(TRACK_ROW).first()).toBeVisible({ timeout: 30_000 });
}

async function _visibleStableIds(page: Page): Promise<string[]> {
	const stableIds = await page.locator(TRACK_ROW).evaluateAll((rows) =>
		rows.map((row) => row.getAttribute('data-stable-id'))
	);
	if (stableIds.some((stableId) => stableId === null)) {
		throw new Error('a mounted track row has no data-stable-id');
	}
	return stableIds as string[];
}

async function _changeTitleSort(page: Page, priorOrder: readonly string[]): Promise<string[]> {
	const priorKey = priorOrder.join('\0');
	const titleHeader = page.locator('th.h-title');
	await titleHeader.click();
	if ((await _visibleStableIds(page)).join('\0') === priorKey) await titleHeader.click();
	await expect.poll(async () => (await _visibleStableIds(page)).join('\0')).not.toBe(priorKey);
	return _visibleStableIds(page);
}

/**
 * Load-ONLY interaction: the per-row "Load onto deck N" button
 * (`TrackTable.svelte:1098`), never the double-click path, which always
 * implies play (see this file's header). Revealed by hover AND row-selected
 * together (hover alone used to block visibility, which
 * is why this now clicks the row - the app's own way of selecting it -
 * before hovering to reveal the box).
 *
 * The reveal is now scoped to hovering `.c-art` or `.c-title` specifically,
 * not the row as a whole: the box moved off `.c-preview`
 * onto `.c-title` so its hitbox can never sit over the mini preview strip,
 * and the CSS trigger narrowed to match (`tr.rb-row-selected:has(.c-art:hover,
 * .c-title:hover) .deck-btns`). `row.hover()` targets the row's bounding-box
 * centre, which - same caveat as the selecting click below - can land on any
 * column depending on persisted widths, so this hovers `.c-title` explicitly
 * rather than the row.
 *
 * The selecting click also lands on the TITLE cell, not the row's own
 * centre: `row.click()` targets the centre of the row box, and which `<td>`
 * sits there depends on the user's persisted column widths (every column is
 * resizable, `onColResizeStart`). One of the columns it can reach is
 * `.c-rating`, whose `RatingStars` writes a rating through `onrate` on
 * click, so a centre click is one column-width change away from silently
 * mutating this fixture's data mid-smoke. `.c-title` carries no handler of
 * its own and selects purely by bubbling to the row.
 *
 * The selection is then ASSERTED rather than assumed. While the box is
 * hidden it is `opacity: 0; pointer-events: none` but still laid out, so
 * Playwright reports the button "visible, enabled and stable" and burns the
 * entire 120s test timeout retrying a click nothing underneath will ever
 * deliver -- which is exactly how this regression presented. A missing
 * `rb-row-selected` names the real cause in seconds instead.
 */
async function _clickLoadOnly(page: Page, index: number, deck = 1): Promise<string> {
	const row = page.locator(TRACK_ROW).nth(index);
	const stableId = await row.getAttribute('data-stable-id');
	if (stableId === null) throw new Error(`row ${index} has no data-stable-id`);
	const title = row.locator('td.c-title');
	await title.click();
	await expect(row, 'the row did not select, so the quick-load box stays hidden').toHaveClass(
		/rb-row-selected/
	);
	await title.hover();
	await row.locator(`button[title="Load onto deck ${deck}"]`).click();
	return stableId;
}

/**
 * Wait until deck 1 has SETTLED on the SPECIFIC track clicked: `stable_id`
 * equals `expectedStableId`, `transport_pending` is clear, and
 * `stems.status` has left `'loading'` for a terminal value -- HELD across
 * `DECK_SETTLE_POLLS` consecutive polls. A single sample is not enough -- a
 * deck mid-swap can report "loaded and not pending" for one poll on its way
 * through (#774). Asserting stable_id equality rather than merely non-null
 * also catches a regression that dispatches or publishes the WRONG track,
 * and waiting out `stems.status` catches the deferred `_upgradeDeckStems()`
 * work `load()` kicks off without awaiting (audio-engine.svelte.ts:3231) --
 * both flagged on review, PR #860. `toastSeen` is sampled on EVERY poll
 * (not just at the end), since an error from that deferred stems path can
 * arrive, and the toast it raises can auto-dismiss, well inside this
 * function's own wait window. Returns rather than throws, so a caller can
 * assert either direction (settled / never-settled) without a try/catch
 * race.
 */
async function _waitForDeckReady(
	page: Page,
	expectedStableId: string
): Promise<{ settled: boolean; toastSeen: boolean }> {
	const deadline = Date.now() + DECK_SETTLE_TIMEOUT_MS;
	let settled = 0;
	for (;;) {
		const sample = await _queryDeckReady(page, 1);
		if (sample.toastSeen) return { settled: false, toastSeen: true };
		const ready =
			sample.stableId === expectedStableId &&
			!sample.transportPending &&
			sample.stemsStatus !== 'loading';
		settled = ready ? settled + 1 : 0;
		if (settled >= DECK_SETTLE_POLLS) return { settled: true, toastSeen: false };
		if (Date.now() > deadline) return { settled: false, toastSeen: false };
		await page.waitForTimeout(DECK_SETTLE_POLL_MS);
	}
}

test.beforeEach(async ({ page }) => {
	// The app's real persistence, not a stub: a prefs blob it would have
	// written itself, so the library opens deterministically (matches
	// webkit-deckload.spec.ts's beforeAll).
	await page.addInitScript((prefsKey) => {
		window.localStorage.setItem(prefsKey, JSON.stringify({ hide_broken_links: false }));
	}, PREFS_STORAGE_KEY);
	await page.goto('/performance');
	await _waitForIpc(page);
	await _openAllTracks(page);
});

test('deck-load smoke: loading a track into deck 1 settles with no toast', async ({ page }) => {
	const stableId = await _clickLoadOnly(page, 0);

	const { settled, toastSeen } = await _waitForDeckReady(page, stableId);
	expect(toastSeen, 'a toast appeared during load or the deferred stems work after it').toBe(
		false
	);
	expect(settled, 'deck 1 never settled (load + stems) on the clicked track').toBe(true);
});

test('library panel toggle starts expanded after the real library load', async ({ page }) => {
	const toggle = page.getByRole('button', { name: 'Hide Next / Recommended panels' });
	await expect(toggle).toBeVisible();
	await toggle.click();
	await expect(
		page.getByRole('button', { name: 'Show Next / Recommended panels' })
	).toBeVisible();
	await expect(page.locator('[data-testid="track-table"]')).toBeVisible();
});

// The picker sits above its row: on the
// row's own line it covered the title's right-hand side and, because each
// target stops dblclick propagation, it swallowed the row's own double-click.
// So the "inside its title cell" half of this test became "above its own row";
// the half that matters unchanged - it must still never reach the FOLLOWING
// row - is asserted exactly as before. `performance-deck-loader-placement.spec.ts`
// carries the full placement/visibility contract; this keeps the check on the
// two-row webkit fixture, where the picker sits over the sticky header.
test('deck picker floats above its own row and cannot intercept the following row', async ({
	page
}) => {
	const selectedRow = page.locator(TRACK_ROW).nth(0);
	const selectedTitle = selectedRow.locator('td.c-title');
	const followingRow = page.locator(TRACK_ROW).nth(1);
	const followingTitle = followingRow.locator('td.c-title');
	const picker = selectedRow.locator('.deck-btns');

	await selectedTitle.click();
	await selectedTitle.hover();
	await expect(picker).toBeVisible();

	const geometry = await page.evaluate(() => {
		const selectedTitle = document.querySelector<HTMLElement>(
			'[data-testid="track-row"].rb-row-selected td.c-title'
		);
		const followingTitle = document.querySelectorAll<HTMLElement>(
			'[data-testid="track-row"] td.c-title'
		)[1];
		if (selectedTitle === null) throw new Error('the fixture did not select a title cell');
		const picker = selectedTitle.querySelector<HTMLElement>('.deck-btns');
		if (picker === null || followingTitle === undefined) {
			throw new Error('the fixture did not render two title cells and a selected deck picker');
		}
		const title = selectedTitle.getBoundingClientRect();
		const box = picker.getBoundingClientRect();
		const following = followingTitle.getBoundingClientRect();
		const hit = document.elementFromPoint(following.x + following.width / 2, following.y + following.height / 2);
		return {
			pickerAboveOwnRow: box.bottom <= title.top + 1 && box.height > 0,
			pickerClearOfFollowingRow: box.bottom <= following.top,
			followingTitleReceivesPointer: hit === followingTitle || followingTitle.contains(hit)
		};
	});
	expect(
		geometry.pickerAboveOwnRow,
		'the rendered picker must sit above its own row, never on the track line'
	).toBe(true);
	expect(geometry.pickerClearOfFollowingRow, 'the picker must not overlap the following row').toBe(
		true
	);
	expect(
		geometry.followingTitleReceivesPointer,
		'the following title must remain the pointer target while the prior picker is active'
	).toBe(true);

	await followingTitle.click();
	await expect(followingRow).toHaveClass(/rb-row-selected/);
	await expect(selectedRow).not.toHaveClass(/rb-row-selected/);
});

test('PLAY-04: active AutoPlay reports sort drift and clears after re-snapshot', async ({
	page
}) => {
	const activationOrder = await _visibleStableIds(page);
	expect(activationOrder.length, 'the real ingest fixture must expose both ordered tracks').toBe(2);

	const autoPlay = page.getByRole('button', { name: 'AutoPlay', exact: true });
	await expect(autoPlay).toHaveAttribute('aria-pressed', 'true');
	await _changeTitleSort(page, activationOrder);

	const notice = page.getByRole('status').filter({ hasText: AUTOPLAY_SNAPSHOT_NOTICE });
	await expect(notice).toBeVisible();
	await expect(notice).toHaveText(AUTOPLAY_SNAPSHOT_NOTICE);

	await autoPlay.click();
	await expect(autoPlay).toHaveAttribute('aria-pressed', 'false');
	await expect(notice).toBeHidden();

	await autoPlay.click();
	await expect(autoPlay).toHaveAttribute('aria-pressed', 'true');
	await expect(notice).toBeHidden();

	const resnapshotOrder = await _visibleStableIds(page);
	await _changeTitleSort(page, resnapshotOrder);
	await expect(notice).toBeVisible();
});

test('deck-load smoke: the real /anlz path never returns the #767 shape (no interception)', async ({
	page
}) => {
	// No page.route() here. This hits the real backend route for this
	// fixture's real (unmapped) track and pins its actual, current contract
	// -- 200, JSON, one of the two documented `local_waveform.status`
	// values. If this route's hardening (see this file's header) ever
	// regresses to where a genuine decode fault leaks past
	// `local_anlz_payload`'s catch, this goes red before anyone has to
	// re-derive why the #767 fault-injection case below is marked
	// UNAVAILABLE instead of exercised. No deck-load involved: just the
	// real row this fixture already rendered, and a direct request through
	// the page's own authenticated context.
	const row = page.locator(TRACK_ROW).first();
	const stableId = await row.getAttribute('data-stable-id');
	if (stableId === null) throw new Error('first row has no data-stable-id');

	const response = await page.request.get(`/api/v1/tracks/${stableId}/anlz?points=100`);
	expect(response.status(), 'real /anlz call did not return 200').toBe(200);
	expect(
		response.headers()['content-type'] ?? '',
		'real /anlz call did not return JSON'
	).toContain('application/json');

	const body = await response.json();
	expect(['decoded', 'not_decoded'], 'unexpected local_waveform.status shape').toContain(
		body.local_waveform?.status
	);
});

test('deck-load smoke: a text/plain 500 from /anlz during load (#767 shape) -- UNAVAILABLE', async ({
	page
}, testInfo) => {
	testInfo.skip(
		true,
		'No code path this deck-load reaches can currently produce a genuine non-JSON 500 for ' +
			"this fixture: local_waveform.py's #735 hardening converts every reachable ffmpeg " +
			'failure mode into a 200 not_decoded response before it reaches app.py, and no other ' +
			'rb API call on this path has an uncaught exception left either. Two substitutes were ' +
			'tried and rejected on review as simulated responses that cannot detect a real-backend ' +
			'regression (a page.route() fulfill, then a globalThis.fetch-stubbed unit test) -- ' +
			'AGENTS.md:L55-L59 asks that an unreachable real path be reported UNAVAILABLE rather ' +
			'than covered by a substitute, so this scenario is not exercised anywhere in this PR ' +
			'(PR #860). tests/unit/api-rb-base.test.mjs keeps a regression pin on ' +
			'`_throwRbApiError` itself (it still throws, never swallows, on a non-JSON body) but ' +
			'is not offered as coverage of this scenario. Re-enable this case only if a route ' +
			'exists whose production code can genuinely raise an uncaught exception for a fixture ' +
			'in this suite.'
	);
});
