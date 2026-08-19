/**
 * The ways back into setup, driven through a real browser.
 *
 * The unit suite (tests/unit/setup-entry-points.test.mjs) executes the shared
 * decision module for real and pins the markup by source shape. What it
 * CANNOT do is press Cmd+, and watch a dialog appear, which is the whole
 * claim of feature 1: a keyboard accelerator that the sidebar button only
 * advertises. So that is what this suite does, end to end, with no stubs.
 *
 * WHY THIS FILE HAS NO CONFIG OF ITS OWN. It is a tier-1 suite: same dev
 * server, same daemon, same viewport as smoke.spec.ts and library.spec.ts, so
 * it is picked up by the root playwright.config.ts along with them. It is ALSO
 * named by playwright.webkit-deckload.config.ts, which runs it under webkit
 * against the engine-served production build, because that is the only place
 * the accelerator meets the production transform and the only place the build
 * identity chip states an address that was ever in doubt.
 *
 * WHY EVERY NAVIGATION GOES THROUGH gotoShellReady. This is an SPA
 * (`ssr = false`, static fallback index.html), so `page.goto` resolves on the
 * load event of a document whose body is still EMPTY, and the Cmd+, listener
 * is installed by the ROOT LAYOUT's onMount. A chord pressed in that gap is
 * delivered to a window with no listener and is silently lost. Under vite the
 * gap is small enough to get away with; under webkit against the production
 * bundle it is not, and two of these tests failed exactly that way while a
 * third doing the same thing on the same route passed.
 *
 * The gate is NOT a sleep and NOT a retried keypress. toggleSettings() is a
 * TOGGLE, so a second chord would close the dialog the first one opened, and
 * a retry loop would convert a real regression into a coin flip. It waits for
 * `window.__mdtPerfLog`, which the root layout installs via
 * startAppInstruments() on the line AFTER installSettingsHotkeys() in the same
 * onMount. That ordering is the whole point: if the global is there, the
 * listener is there. A broken accelerator still fails, because the gate only
 * removes the race, never the assertion.
 *
 * Requirements:
 *
 * - ✔︎ Cmd+, opens the settings surface from the app shell AND from
 *   /performance, because the accelerator is installed at the root layout.
 * - ✔︎ "Run setup" from that surface lands on /setup with the wizard rendered.
 * - ✔︎ The /admin Setup tab lands on /setup.
 * - ✔︎ No console errors on the way, so a working navigation cannot hide a
 *   broken request.
 *
 * Acceptance tests:
 *
 * - [if] Cmd+, does nothing on /performance [then ⛔️] the accelerator is
 *   label-only outside the app shell.
 * - [if] "Run setup" is present but inert [then ⛔️] the entry point is
 *   decoration.
 * - [if] the Setup tab navigates anywhere other than /setup [then ⛔️] the
 *   operator panel's route into setup is wrong.
 * - [if] /setup renders its "daemon not identified yet" refusal after either
 *   entry point [then ⛔️] the capability probe is being raced again.
 * - [if] a chord is pressed before the root layout's onMount has run [then ⛔️]
 *   the suite reports a lost keystroke as a missing accelerator.
 */
import { test, expect, type Page } from '@playwright/test';

/** The chord, spelled once. Playwright maps Meta to Command on macOS and to
 * the Windows key elsewhere; the handler accepts either meta or ctrl, so the
 * platform-correct modifier is what gets pressed. */
const SETTINGS_CHORD = process.platform === 'darwin' ? 'Meta+Comma' : 'Control+Comma';

const settingsDialog = (page: Page) => page.getByRole('dialog', { name: 'Settings' });
const runSetupButton = (page: Page) => page.getByRole('button', { name: 'Run setup', exact: true });

/** The sentence that means the capability probe was read before it answered.
 * It must never be on screen after a deliberate navigation into setup. */
const PROBE_RACE = /daemon not identified yet/;

/**
 * Navigate, then wait until the root layout's onMount has actually run.
 *
 * `__mdtPerfLog` is installed by startAppInstruments(), which the root layout
 * calls on the line after installSettingsHotkeys(). Waiting for it therefore
 * proves the keydown listener is on window, which `page.goto` alone does not:
 * in SPA mode goto resolves against an empty body.
 */
async function gotoShellReady(page: Page, path: string): Promise<void> {
	await page.goto(path);
	await page.waitForFunction(
		() => typeof (window as unknown as { __mdtPerfLog?: unknown }).__mdtPerfLog === 'function',
		undefined,
		{ timeout: 30_000 }
	);
}

async function expectWizard(page: Page): Promise<void> {
	await expect(page).toHaveURL(/\/setup\/?$/);
	// The page heading, plus the step the wizard opens on. "Welcome" is a step
	// label in the stepper, not a heading, so it is matched as text.
	await expect(page.getByRole('heading', { name: 'First-run setup' })).toBeVisible();
	await expect(page.locator('.steps .step').first()).toContainText('Welcome');
	await expect(page.getByText(PROBE_RACE)).toHaveCount(0);
}

test.describe('setup entry points', () => {
	test('Cmd+, opens settings, and Run setup lands on the wizard', async ({ page }) => {
		const errors: string[] = [];
		page.on('console', (msg) => {
			if (msg.type() === 'error') errors.push(msg.text());
		});

		await gotoShellReady(page, '/');
		await expect(settingsDialog(page)).toHaveCount(0);

		await page.keyboard.press(SETTINGS_CHORD);
		await expect(settingsDialog(page)).toBeVisible();

		await runSetupButton(page).click();
		await expectWizard(page);

		expect(errors.filter((e) => !e.includes('favicon'))).toEqual([]);
	});

	test('the accelerator also works on /performance', async ({ page }) => {
		// The hotkey is installed from the ROOT layout precisely because
		// /performance bypasses the app shell and its sidebar button.
		await gotoShellReady(page, '/performance');
		await page.keyboard.press(SETTINGS_CHORD);
		await expect(settingsDialog(page)).toBeVisible();
		await expect(runSetupButton(page)).toBeEnabled();
	});

	test('the Run setup action is reachable before typing anything', async ({ page }) => {
		// It lives outside the collapsible body on purpose. An entry point you
		// can only reach by first searching for it is not an entry point.
		await gotoShellReady(page, '/');
		await page.keyboard.press(SETTINGS_CHORD);
		await expect(runSetupButton(page)).toBeVisible();
	});

	test('the admin Setup tab lands on the wizard', async ({ page }) => {
		await gotoShellReady(page, '/admin');
		const tabs = page.getByRole('tablist', { name: 'admin sections' });
		await expect(tabs).toBeVisible();
		await expect(tabs.getByRole('tab', { name: 'KPI ledger' })).toHaveAttribute(
			'aria-selected',
			'true'
		);
		await tabs.getByRole('tab', { name: 'Setup' }).click();
		await expectWizard(page);
	});

	test('the build identity chip states this app address in its foldout', async ({ page }) => {
		// The reason the chip moved into the tray at all: a tester could not
		// find the packaged app's URL, because the engine binds an ephemeral
		// port and nothing on screen said which one.
		await gotoShellReady(page, '/');
		const chip = page.locator('.build-identity');
		await expect(chip).toBeVisible();
		await chip.getByRole('button').first().click();
		const url = chip.locator('code.url');
		await expect(url).toBeVisible();
		await expect(url).toHaveText(/^https?:\/\/[^\s]+$/);
		await expect(chip.getByRole('button', { name: 'copy' })).toBeVisible();
	});
});
