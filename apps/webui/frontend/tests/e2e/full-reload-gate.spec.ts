/**
 * REFRESH-01: a REAL Vite dev full reload is held behind the countdown
 * overlay, then lands (PR #890 review r3920753724).
 *
 * `reload-countdown-browser.spec.ts`'s "dev full-reload receiver" test only
 * proves `src/lib/rb/dev-full-reload-receiver.ts` loaded as a real file
 * request with a working `import.meta.hot` - useful, but it never causes
 * Vite to emit an actual `full-reload`, so it stays green even if
 * `../../vite-hold-full-reload.ts`'s `server.hot.send` interception breaks
 * or the custom event it rewrites `full-reload` into never reaches this
 * listener. This test exercises the real path end to end instead.
 *
 * ## Triggering a genuine full-reload deterministically
 *
 * The receiver module itself is the trigger. It is loaded only via a
 * `<script type="module" src="...">` tag `src/hooks.server.ts` injects
 * (dev-full-reload-receiver.ts's own docstring has the full trace of why),
 * never `import`-ed by another module - so Vite's module graph records zero
 * importers for it. It also never calls `import.meta.hot.accept()`; it only
 * registers an `.on()` listener and sets a flag. Vite's HMR algorithm walks
 * up from a changed file looking for an accepting boundary; with no
 * self-accept and no importers to walk up TO, there is nowhere for the
 * update to land, and Vite's own behaviour for that shape - confirmed live
 * against this project's real dev server this session, the same way this
 * repo's other e2e docstrings record what they actually observed rather than
 * assumed - is a genuine `full-reload` broadcast, exactly the payload
 * `holdFullReloadPlugin()` exists to intercept and hold.
 *
 * ## Why this needs its own server (see playwright.full-reload-gate.config.ts)
 *
 * The broadcast goes to every client connected to the dev server, and every
 * page in every spec already has this same receiver injected. Editing this
 * file against the shared playwright.config.ts server would schedule a
 * reload on whatever OTHER spec's page happened to be open in another
 * worker at that moment. A dedicated server keeps the blast radius to this
 * suite alone.
 */
import { expect, test } from '@playwright/test';
import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

const RECEIVER_PATH = fileURLToPath(
	new URL('../../src/lib/rb/dev-full-reload-receiver.ts', import.meta.url)
);

test.describe('a real Vite full reload, held behind the countdown overlay', () => {
	test('the overlay appears before the reload, and the reload still lands', async ({ page }) => {
		await page.goto('/');
		await page.evaluate(async () => {
			const mod = await import(new URL('/src/lib/rb/reload-countdown.ts', location.href).href);
			mod.installReloadCountdown();
		});

		// Confirms the receiver is live BEFORE the probe touch, the same
		// precondition reload-countdown-browser.spec.ts's cheaper test checks.
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

		const original = await readFile(RECEIVER_PATH, 'utf8');
		try {
			// A no-op comment touch: it changes the file's content (so Vite's
			// watcher fires and the module graph considers it stale) without
			// changing anything the module actually does.
			await writeFile(RECEIVER_PATH, `${original}\n// full-reload-gate probe touch\n`);

			const overlay = page.locator('#mdt-reload-countdown');
			await expect(overlay).toBeVisible({ timeout: 10_000 });
			await expect(overlay).toContainText('vite');

			// The real, held reload still lands once the countdown elapses -
			// this is what a dropped interception would skip straight past.
			// RELOAD_COUNTDOWN_S (reload-countdown.ts) is 10s of real ticks, so
			// the wait needs headroom past that, not just past the assertions above.
			await page.waitForEvent('load', { timeout: 15_000 });
		} finally {
			// Restored unconditionally: a failed assertion above must not leave
			// a real source file mutated on disk for the next run or reviewer.
			await writeFile(RECEIVER_PATH, original);
		}
	});
});
