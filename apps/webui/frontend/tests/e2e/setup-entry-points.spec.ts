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
 * it is picked up by the root playwright.config.ts along with them. If a
 * webkit project is added to that config later, this suite gains webkit
 * coverage with no edit here, which is the point of not forking a config.
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

		await page.goto('/');
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
		await page.goto('/performance');
		await page.keyboard.press(SETTINGS_CHORD);
		await expect(settingsDialog(page)).toBeVisible();
		await expect(runSetupButton(page)).toBeEnabled();
	});

	test('the Run setup action is reachable before typing anything', async ({ page }) => {
		// It lives outside the collapsible body on purpose. An entry point you
		// can only reach by first searching for it is not an entry point.
		await page.goto('/');
		await page.keyboard.press(SETTINGS_CHORD);
		await expect(runSetupButton(page)).toBeVisible();
	});

	test('the admin Setup tab lands on the wizard', async ({ page }) => {
		await page.goto('/admin');
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
		await page.goto('/');
		const chip = page.locator('.build-identity');
		await expect(chip).toBeVisible();
		await chip.getByRole('button').first().click();
		const url = chip.locator('code.url');
		await expect(url).toBeVisible();
		await expect(url).toHaveText(/^https?:\/\/[^\s]+$/);
		await expect(chip.getByRole('button', { name: 'copy' })).toBeVisible();
	});
});
