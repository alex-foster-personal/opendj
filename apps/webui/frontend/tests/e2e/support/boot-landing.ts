/**
 * Spend PERFMODE-11's one-shot cold-open redirect before a spec opens `/`.
 *
 * 67f19c2ee made a COLD open of `/` land in Gig: the root layout redirects to
 * /performance once per browser session while LIBRARY_MODE_SHIPPED is false.
 * Every Playwright test gets a fresh context, so every `page.goto('/')` in the
 * root suite is a cold open and RACES that redirect. The race is why the same
 * app-shell specs report differently on the same commit depending on where
 * they run: at c1c088d80 `library.spec.ts:4` and `track-detail.spec.ts:4` won
 * the race on CI and lost it on a developer Mac, while
 * `header-status-strip.spec.ts:25` and `:54` lost it in both.
 *
 * The redirect is deliberately one-shot and session-gated precisely so that
 * navigating to the library AFTER first open is untouched, and that is the
 * state every app-shell spec means to be in. This helper reaches it through
 * the real redirect: it cold-opens `/`, waits for the landing route, checks
 * the app itself wrote the session flag, then leaves the tab on about:blank so
 * the landing page's requests abort before the caller attaches listeners.
 * sessionStorage survives same-tab navigation, so the caller's next
 * `page.goto` is a warm open.
 *
 * The rule itself keeps its own coverage: `gig-boot-landing.spec.ts` clears
 * the same flag and asserts the redirect, and `tests/unit/boot-landing.test.mjs`
 * owns the decision function.
 */
import { expect, type Page } from '@playwright/test';

import { resolveBootLandingRoute } from '../../../src/lib/rb/app-mode-landing';
import { BOOT_LANDING_SESSION_KEY } from '../../../src/lib/rb/boot-landing';

export async function spendBootLanding(page: Page): Promise<void> {
	// A fresh context has no Gig stamp, so the landing is the no-stamp route.
	const landing = resolveBootLandingRoute(null, Date.now());
	await page.goto('/', { waitUntil: 'domcontentloaded' });
	if (landing !== '/') {
		await page.waitForURL((url) => url.pathname === landing, { waitUntil: 'domcontentloaded' });
	}
	// The app, not the test, wrote the flag: fail loud if the redirect never ran.
	await expect
		.poll(() =>
			page.evaluate((key) => window.sessionStorage.getItem(key), BOOT_LANDING_SESSION_KEY)
		)
		.not.toBeNull();
	await page.goto('about:blank');
}
