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
 * state every app-shell spec means to be in. Setting the session flag up front
 * puts them there, so they exercise the app shell rather than the landing rule.
 *
 * The rule itself keeps its own coverage and is NOT weakened by this:
 * `gig-boot-landing.spec.ts` clears the same flag and asserts the redirect,
 * and `tests/unit/boot-landing.test.mjs` owns the decision function.
 */
import type { Page } from '@playwright/test';

import { BOOT_LANDING_SESSION_KEY } from '../../../src/lib/rb/boot-landing';

export async function spendBootLanding(page: Page): Promise<void> {
	await page.addInitScript((key) => {
		window.sessionStorage.setItem(key, '1');
	}, BOOT_LANDING_SESSION_KEY);
}
