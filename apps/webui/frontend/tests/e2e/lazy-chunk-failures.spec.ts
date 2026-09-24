/**
 * The two root-layout chunks that load off the first paint fail VISIBLY,
 * proven through the real runtime path (Codex review of #3862, P1 and P2).
 *
 * `tests/unit/feedback-pin-shell-boot.test.mjs` runs `deferFeedbackPinShell`
 * for real but hands it its own scheduler and loader, and pins the layout's
 * wiring by source shape. That is the fast contract of the seam; it is not
 * proof that the production scheduler releases the real `import()`, that the
 * real rejection reaches the layout, or that the toast and the marked slot
 * are actually rendered. This suite is that proof: the real layout in a real
 * browser, the real boot scheduler, the real dynamic imports, and the ONE
 * thing a stale deploy or a dropped connection does to them, which is that
 * the chunk request fails. Nothing is stubbed in the app: `page.route` aborts
 * the request at the network layer, exactly where the real failure happens.
 *
 * Regression lines:
 * - if a rejected pin-shell chunk stops raising the error toast and the "!"
 *   slot then the feature vanishes silently again
 * - if a rejected setup chunk renders nothing then a fresh install is
 *   stranded behind a yielded preflight gate with no wizard
 * - if the retry stops loading a fresh document then it retries nothing: a
 *   browser keeps a failed module fetch in its module map, so a second
 *   import() of the same URL rejects with no request on the wire (measured
 *   in Chromium while writing this: the first shape of this fix re-created
 *   the promise, drew a second toast, and never touched the network)
 */
import { expect, test, type Page } from '@playwright/test';
import { BOOT_IDLE_TIMEOUT_MS, BOOT_QUIET_MS, DECK_LOAD_YIELD_MAX_MS } from '../../src/lib/rb/boot-scheduler';

/** How long the failure surface may take to appear after the shell is up.
 *
 * The scheduler releases deferred boot work only after BOOT_QUIET_MS, an
 * idle frame capped at BOOT_IDLE_TIMEOUT_MS, and up to DECK_LOAD_YIELD_MAX_MS
 * of yielding while decks are loading (the Performance page restores decks
 * on boot), so the chunk request itself starts up to that sum after the
 * shell is ready; the abort then rejects it. The margin on top is for the
 * request under a loaded CI box: the first shape of this spec allowed 20 s
 * inside the suite's 30 s per-test budget and the abort landed at the
 * deadline on CI (run 36007918940) while passing locally every time. */
const SHELL_RELEASE_BOUND_MS = BOOT_QUIET_MS + BOOT_IDLE_TIMEOUT_MS + DECK_LOAD_YIELD_MAX_MS;
const FAILURE_SURFACE_MS = SHELL_RELEASE_BOUND_MS + 30_000;
const TEST_BUDGET_MS = 90_000;

/** See setup-entry-points.spec.ts: the root layout installs `__mdtPerfLog`
 * from the same onMount that arms everything this suite waits on. */
async function gotoShellReady(page: Page, path: string): Promise<void> {
	await page.goto(path);
	await page.waitForFunction(
		() => typeof (window as unknown as { __mdtPerfLog?: unknown }).__mdtPerfLog === 'function',
		undefined,
		{ timeout: 30_000 }
	);
}

/** The error toast, read where it is durable: pushToast records every toast
 * in the perf-event ring (`window.__mdtPerfLog`, the same ring the
 * client-error report rides) under `toast-<kind>` with the raw message. The
 * on-screen toast folds that message behind a headline and an expander, and
 * dismisses itself after TOAST_DEFAULT_MS, so the ring is what proves the
 * layout raised it and what it said. */
async function errorToastMessages(page: Page): Promise<string[]> {
	return page.evaluate(() => {
		const ring = (window as unknown as { __mdtPerfLog?: () => readonly { kind: string; message: string }[] })
			.__mdtPerfLog;
		if (ring === undefined) return [];
		return ring()
			.filter((event) => event.kind === 'toast-error')
			.map((event) => event.message);
	});
}

/** The chunk's module URL under vite dev and under the production build both
 * carry the component's file stem, so one glob names it in either. */
const PIN_LAYER_CHUNK = '**/FeedbackPinLayer*';
const SETUP_OVERLAY_CHUNK = '**/SetupOverlay*';

test.describe('lazy root-layout chunks fail visibly', () => {
	test('a pin-shell chunk that cannot be fetched raises an error toast and marks the slot', async ({ page }) => {
		test.setTimeout(TEST_BUDGET_MS);
		const aborted: string[] = [];
		await page.route(PIN_LAYER_CHUNK, (route) => {
			aborted.push(route.request().url());
			return route.abort();
		});
		await gotoShellReady(page, '/');

		// The shell loads behind the boot window (SHELL_RELEASE_BOUND_MS), so
		// this waits on the real scheduler, not on a sleep. The slot is marked
		// in the same callback that raises the toast, and it stays: the toast
		// dismisses itself, so the slot is the durable thing to wait on first.
		const slot = page.getByRole('img', { name: 'Comment pins failed to load' });
		await expect(slot).toBeVisible({ timeout: FAILURE_SURFACE_MS });
		await expect(slot).toHaveAttribute('title', /^Comment pins failed to load: .+Reload the page to retry\.$/);
		await expect(page.locator('.toast-stack .toast.error')).toBeVisible();
		await expect
			.poll(() => errorToastMessages(page))
			.toEqual(expect.arrayContaining([expect.stringMatching(/^Comment pins failed to load: .+/)]));
		expect(aborted.length, 'the pin layer chunk was never requested').toBeGreaterThan(0);
		// The failure is the SHELL's, not the page's: the app behind it is up.
		await expect(page.getByTestId('header-status-strip')).toBeVisible();
	});

	test('a setup chunk that cannot be fetched shows a retry surface, and the retry fetches it again', async ({ page }) => {
		test.setTimeout(TEST_BUDGET_MS);
		let aborted = 0;
		await page.route(SETUP_OVERLAY_CHUNK, (route) => {
			aborted += 1;
			return route.abort();
		});
		// /setup is the door: it opens the overlay and hands the browser on to
		// the host route (setup-entry-points.spec.ts), so the layout awaits the
		// chunk that was already rejected.
		await gotoShellReady(page, '/setup');

		const failed = page.getByRole('alertdialog', { name: 'Setup failed to load' });
		await expect(failed).toBeVisible({ timeout: FAILURE_SURFACE_MS });
		await expect(failed).toContainText('Setup failed to load:');
		await expect(page.locator('.toast-stack .toast.error')).toBeVisible();
		await expect
			.poll(() => errorToastMessages(page))
			.toEqual(expect.arrayContaining([expect.stringMatching(/^Setup failed to load: .+/)]));
		await expect(page.getByRole('dialog', { name: 'First-run setup' })).toHaveCount(0);
		expect(aborted, 'the setup chunk was never requested').toBeGreaterThan(0);

		// Let the chunk through and retry: a fresh document at the /setup door,
		// so the wizard must arrive in it. A second requested chunk is the
		// proof that the retry went to the network at all.
		await page.unroute(SETUP_OVERLAY_CHUNK);
		const requested: string[] = [];
		page.on('request', (request) => {
			if (/SetupOverlay/.test(request.url())) requested.push(request.url());
		});
		await failed.getByRole('button', { name: 'Reload and retry' }).click();
		await expect(page.getByRole('dialog', { name: 'First-run setup' })).toBeVisible({ timeout: 20_000 });
		await expect(page.getByRole('alertdialog', { name: 'Setup failed to load' })).toHaveCount(0);
		expect(requested.length, 'the retry never requested the setup chunk').toBeGreaterThan(0);
	});
});
