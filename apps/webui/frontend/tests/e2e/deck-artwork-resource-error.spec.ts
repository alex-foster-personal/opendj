/**
 * A deck's missing artwork is an EXPECTED miss, not a client error (#5612).
 *
 * DeckHeader and WaveTrackSummary request artwork for every loaded track and
 * fall back to a placeholder when it 404s. Their <img> carries
 * `data-expected-resource-error`, and the resource-error capture in
 * client-error-reporting.ts skips a target that declares it, so a track with
 * no art does not post a "Failed to load img" row on every deck load.
 *
 * Everything here is real: the root config's engine serving the generated
 * fixture library (whose tracks carry no artwork, so the deck artwork route
 * answers an error), the Svelte-rendered deck markup, Chromium's native
 * resource-error dispatch, the app's own capture listener and its POST to
 * /api/v1/client-errors on the engine. Nothing is routed or stubbed.
 *
 * The absence claim has a positive control on the same page and the same
 * path: an UNMARKED image of the same missing artwork, added afterwards, must
 * post. The reporter flushes its queue in order, one POST at a time, so had a
 * deck artwork error been reported it would have been posted before the
 * control's.
 *
 * RUN. Root `playwright.config.ts` (generated fixture library, engine + vite
 * on the worktree's claimed port pair):
 *   pnpm exec playwright test deck-artwork-resource-error
 *
 * Acceptance:
 *   [if] a loaded track's deck artwork fails to load [then STOP] a
 *        resource-error row is posted to /api/v1/client-errors for it
 *   [if] a deck artwork <img> that failed lacks data-expected-resource-error
 *        [then STOP] the marker never reached the rendered DOM
 *   [if] an unmarked image fails on the same page [then] it IS posted, or the
 *        capture was not live and the absence above proves nothing
 */
import { expect, test } from '@playwright/test';

/** Library UI prefs only (as destination-audible.spec.ts): the generated
 * fixture tracks must not be hidden as broken links. */
const PREFS_STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const TRACK_ROW = '[data-testid="track-row"]';
const MARKER = 'data-expected-resource-error';
const CONTROL_PARAM = 'expected-error-control=1';

interface FailedImage {
	src: string;
	marked: boolean;
}

type ErrorWindow = Window & { __deckArtworkErrors?: FailedImage[] };

/** A deck artwork URL (deckArtworkUrl in api-rb.ts) for `stableId`. */
function isDeckArtwork(src: string, stableId: string): boolean {
	return src.includes(`/tracks/${stableId}/artwork`) && src.includes('online=true');
}

test('a deck artwork error is not reported, while an unmarked one on the same path is', async ({
	page
}) => {
	test.setTimeout(90_000);
	await page.addInitScript(
		({ prefsKey, marker }) => {
			window.localStorage.setItem(prefsKey, JSON.stringify({ hide_broken_links: false }));
			// Registered before any app script, in the capture phase like the app's
			// own listener: what each failing <img> looked like when it failed.
			const seen: { src: string; marked: boolean }[] = [];
			(window as Window & { __deckArtworkErrors?: typeof seen }).__deckArtworkErrors = seen;
			window.addEventListener(
				'error',
				(event) => {
					const target = event.target;
					if (target instanceof HTMLImageElement) {
						seen.push({ src: target.src, marked: target.hasAttribute(marker) });
					}
				},
				true
			);
		},
		{ prefsKey: PREFS_STORAGE_KEY, marker: MARKER }
	);
	const posted: string[] = [];
	page.on('request', (request) => {
		if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/v1/client-errors') {
			posted.push(request.postData() ?? '');
		}
	});

	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto('/performance?playlist=all');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const firstRow = page.locator(TRACK_ROW).first();
	await expect(firstRow).toBeVisible({ timeout: 30_000 });
	const stableId = await firstRow.getAttribute('data-stable-id');
	expect(stableId, 'fixture library must expose a track row').toBeTruthy();
	const sid = stableId as string;
	await page.evaluate(async (id) => {
		await window.musicDjToolsPerformance!.dispatch({ type: 'load', deck: 1, stable_id: id });
	}, sid);

	const deckFailures = async (): Promise<FailedImage[]> =>
		(await page.evaluate(() => (window as ErrorWindow).__deckArtworkErrors ?? [])).filter((row) =>
			isDeckArtwork(row.src, sid)
		);
	await expect
		.poll(async () => (await deckFailures()).length, {
			message: 'the loaded track has no artwork, so its deck artwork must fail to load',
			timeout: 30_000
		})
		.toBeGreaterThan(0);
	const failures = await deckFailures();
	expect(
		failures.filter((row) => !row.marked),
		'every deck artwork <img> that failed must carry the expected-error marker'
	).toEqual([]);

	// Positive control: the same missing artwork, unmarked, on the same page.
	const controlSrc = `${failures[0].src}&${CONTROL_PARAM}`;
	const controlPosted = page.waitForRequest(
		(request) =>
			request.method() === 'POST' &&
			new URL(request.url()).pathname === '/api/v1/client-errors' &&
			(request.postData() ?? '').includes(CONTROL_PARAM),
		{ timeout: 15_000 }
	);
	await page.evaluate((src) => {
		const control = document.createElement('img');
		control.alt = '';
		control.src = src;
		document.body.append(control);
	}, controlSrc);
	const controlBody = (await controlPosted).postData() ?? '';
	expect(controlBody, 'the control posts as a resource error').toContain('resource-error');

	const deckReports = posted.filter(
		(body) => body.includes(`/tracks/${sid}/artwork`) && !body.includes(CONTROL_PARAM)
	);
	expect(deckReports, 'a marked deck artwork failure was posted as a client error').toEqual([]);
});
