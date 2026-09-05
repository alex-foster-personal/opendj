import { expect, test } from '@playwright/test';

/**
 * Exercises `installReloadCountdown` (src/lib/rb/reload-countdown.ts)
 * through the real browser DOM, real `setInterval`, and a real
 * `location.reload()`, replacing tests/unit/reload-countdown.test.mjs's
 * injected fake visibility/reload/timer/render/clear effects, which only
 * proved the isolated scheduler calls its own test doubles (r3915558866).
 *
 * No backend needed: installReloadCountdown has no fetching side effects,
 * so this runs under the default (vite-only) playwright.config.ts.
 */

test.describe('reload countdown, wired to the real page', () => {
	test('counts down in the real overlay and reloads the real page', async ({ page }) => {
		await page.goto('/');
		await page.evaluate(async () => {
			const mod = await import(new URL('/src/lib/rb/reload-countdown.ts', location.href).href);
			mod.installReloadCountdown();
		});

		// A real production call: the one exposed for agent-native parity.
		await page.evaluate(() => {
			(window as unknown as { __mdtScheduleReload: (r: string, s?: number) => void }).__mdtScheduleReload(
				'e2e test',
				2
			);
		});

		const overlay = page.locator('#mdt-reload-countdown');
		await expect(overlay).toBeVisible();
		await expect(overlay).toContainText('2');
		await expect(overlay).toContainText('reloading: e2e test');

		// Real setInterval ticking in a real document - not a fake clock.
		await expect(overlay.locator('div').first()).toHaveText('1', { timeout: 1500 });

		// The real location.reload() firing IS the assertion the fake-effects
		// harness could never make: navigation actually happens.
		await page.waitForEvent('load', { timeout: 3000 });
		expect(page.url()).toContain('/');
	});

	test('teardown cancels a running countdown - no reload follows', async ({ page }) => {
		await page.goto('/');
		let navigated = false;
		page.on('load', () => {
			navigated = true;
		});

		await page.evaluate(async () => {
			const mod = await import(new URL('/src/lib/rb/reload-countdown.ts', location.href).href);
			const teardown = mod.installReloadCountdown();
			(window as unknown as { __mdtTeardown?: () => void }).__mdtTeardown = teardown;
			(window as unknown as { __mdtScheduleReload: (r: string, s?: number) => void }).__mdtScheduleReload(
				'should never fire',
				1
			);
		});
		await expect(page.locator('#mdt-reload-countdown')).toBeVisible();

		await page.evaluate(() => {
			(window as unknown as { __mdtTeardown: () => void }).__mdtTeardown();
		});
		await expect(page.locator('#mdt-reload-countdown')).toHaveCount(0);

		// Wait past what the cancelled countdown's deadline would have been.
		await page.waitForTimeout(1500);
		expect(navigated).toBe(false);
	});

	test('a second trigger mid-countdown joins the first deadline - only one reload follows', async ({
		page
	}) => {
		await page.goto('/');
		let loads = 0;
		page.on('load', () => {
			loads += 1;
		});

		await page.evaluate(async () => {
			const mod = await import(new URL('/src/lib/rb/reload-countdown.ts', location.href).href);
			mod.installReloadCountdown();
		});
		await page.evaluate(() => {
			(window as unknown as { __mdtScheduleReload: (r: string, s?: number) => void }).__mdtScheduleReload(
				'first',
				2
			);
		});

		const overlay = page.locator('#mdt-reload-countdown');
		await expect(overlay).toContainText('reloading: first');

		// A second, real production call while the first is still counting down.
		// It must not restart the deadline or the reason under a reader's eyes.
		await page.evaluate(() => {
			(window as unknown as { __mdtScheduleReload: (r: string, s?: number) => void }).__mdtScheduleReload(
				'second',
				5
			);
		});
		await expect(overlay).toContainText('reloading: first');

		await page.waitForEvent('load', { timeout: 4000 });
		expect(loads).toBe(1);
	});

	test('a non-positive custom duration reloads immediately with no overlay', async ({ page }) => {
		await page.goto('/');
		await page.evaluate(async () => {
			const mod = await import(new URL('/src/lib/rb/reload-countdown.ts', location.href).href);
			mod.installReloadCountdown();
		});

		const loadPromise = page.waitForEvent('load', { timeout: 3000 });
		// Checked in the SAME evaluate call as the trigger, synchronously after
		// it returns - `expect(locator).toHaveCount(0)` would auto-retry across
		// a round trip and could pass merely because a LATER `clear()` tidied up
		// an overlay that briefly existed. This proves it never existed at all.
		const overlayExistedImmediately = await page.evaluate(() => {
			(window as unknown as { __mdtScheduleReload: (r: string, s?: number) => void }).__mdtScheduleReload(
				'no wait',
				0
			);
			return document.getElementById('mdt-reload-countdown') !== null;
		});
		expect(overlayExistedImmediately).toBe(false);
		await loadPromise;
	});
});

/**
 * r3920676418: the hidden-tab branch (`!fx.isVisible()` in
 * `createReloadScheduler`) is exercised only by tests/unit/reload-countdown.test.mjs's
 * fake-effects harness, not here. That is a genuine capability gap, not an
 * oversight: `document.visibilityState`/`document.hidden` are read-only
 * browser-computed properties, and the repository's no-mocks rule
 * (AGENTS.md#L55-L59) forbids monkeypatching them to fake a backgrounded tab.
 *
 * Four real mechanisms were tried against this project's actual Playwright/
 * Chromium stack and none moved `document.visibilityState` off `'visible'`:
 *   1. A second page opened in the same context and brought to front with
 *      `page.bringToFront()` - the first page stayed `'visible'`.
 *   2. CDP `Page.setWebLifecycleState({state: 'frozen'})` - no effect on
 *      `document.visibilityState`; that command drives the Page Lifecycle
 *      API (freeze/resume), a different spec from Page Visibility.
 *   3. CDP `Browser.setWindowBounds({windowState: 'minimized'})` under
 *      headless Chromium - no effect; headless has no real window to occlude.
 *   4. The same minimize call under `headless: false` inside `xvfb-run` (a
 *      real X server, confirmed present on this host) - still no effect;
 *      Chromium's occlusion tracking needs a compositing window manager,
 *      which a bare Xvfb display does not provide, and CI has neither a WM
 *      nor a display at all.
 *
 * Per AGENTS.md#L55-L59 ("if a dependency is unavailable, fail explicitly or
 * report the capability as unavailable; do not manufacture a passing
 * result"): the capability to make a real Chromium tab genuinely
 * backgrounded is UNAVAILABLE in this CI environment. The hidden-tab branch
 * keeps its real coverage in the pure-scheduler unit test (which is honest
 * about testing the scheduler's branch logic, not the browser's visibility
 * plumbing) until a runner with a real window manager makes a genuine e2e
 * probe possible.
 */

/**
 * r3919761144: the `mdt:full-reload` receiver (../../vite-hold-full-reload.ts
 * rewrites Vite's own full-reload payload into this custom event) used to be
 * injected as an inline `<script type="module">` BODY via a
 * `transformIndexHtml` Vite hook. Two real defects made that dead: SvelteKit's
 * dev middleware never calls `server.transformIndexHtml()` at all, and even
 * where the tag lands directly in a served document, an inline script body
 * (unlike a `src=` file request) never gets Vite's `import.meta.hot`
 * injection - both confirmed directly against this project's real dev server
 * this session. `src/hooks.server.ts` now injects a real `<script
 * type="module" src="...">` pointing at src/lib/rb/dev-full-reload-
 * receiver.ts, which sets `window.__mdtFullReloadReceiverInstalled` the
 * moment its own `import.meta.hot` block actually runs - so this only goes
 * true if the tag was injected into a REAL page AND the loaded file got a
 * REAL, working import.meta.hot, which is exactly what was silently false
 * before this fix.
 */
test.describe('dev full-reload receiver, wired to a real page', () => {
	test('the real hooks.server.ts injection loads a real module with a working import.meta.hot', async ({
		page
	}) => {
		await page.goto('/');
		await expect
			.poll(
				() =>
					page.evaluate(
						() =>
							(window as Window & { __mdtFullReloadReceiverInstalled?: boolean })
								.__mdtFullReloadReceiverInstalled
					),
				{ timeout: 10_000 }
			)
			.toBe(true);
	});
});
