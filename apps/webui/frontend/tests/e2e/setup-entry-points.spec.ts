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
 * WHAT THE WIZARD IS NOW. It is an OVERLAY over the live performance view,
 * not a page. /setup is still a door -- bookmarks, SETUP_ROUTE and agent
 * flows all point at it -- but it opens the dialog and hands the browser on
 * to the host route. So every entry point below is checked for the DIALOG,
 * and the URL it settles on is the app, not a wizard screen.
 *
 * THE FAILURE STATES ARE CHECKED WITHOUT A SINGLE STUB. Under the webkit
 * artifact config the engine runs with HOME pointed at a sandbox directory,
 * so `~/Library/Pioneer/rekordbox/master.db` genuinely is not there and
 * detection genuinely reports `rekordbox_not_found`. Under the chromium/vite
 * config it is the lane engine on a machine that has rekordbox, so detection
 * genuinely succeeds. The suite therefore ASKS the API which world it is in
 * and asserts the matching contract -- real data both times, and the harder
 * half runs exactly where a tester meets it.
 *
 * Requirements:
 *
 * - ✔︎ Cmd+, opens the settings surface from the app shell AND from
 *   /performance, because the accelerator is installed at the root layout.
 * - ✔︎ "Run setup" from that surface opens the setup dialog over the app.
 * - ✔︎ The /admin Setup tab opens the same dialog.
 * - ✔︎ /setup deep-links to the same dialog rather than 404ing or rendering a
 *   second wizard.
 * - ✔︎ The detect step ALWAYS offers three enabled ways forward, whatever
 *   detection found.
 * - ✔︎ A fatal blocker renders as an alert in the danger colour, with the
 *   reason Continue is refused visible INLINE.
 * - ✔︎ No console errors on the way, so a working navigation cannot hide a
 *   broken request.
 *
 * Acceptance tests:
 *
 * - [if] Cmd+, does nothing on /performance [then ⛔️] the accelerator is
 *   label-only outside the app shell.
 * - [if] "Run setup" is present but inert [then ⛔️] the entry point is
 *   decoration.
 * - [if] the Setup tab opens no dialog [then ⛔️] the operator panel's route
 *   into setup is wrong.
 * - [if] the overlay renders its "daemon not identified yet" refusal after
 *   any entry point [then ⛔️] the capability probe is being raced again.
 * - [if] a chord is pressed before the root layout's onMount has run [then ⛔️]
 *   the suite reports a lost keystroke as a missing accelerator.
 * - [if] any of the three escape buttons is disabled on the detect step
 *   [then ⛔️] a first-run user can be dead-ended, which is the whole bug.
 * - [if] a fatal blocker's sentence is not red [then ⛔️] the failure reads as
 *   ordinary prose, which is exactly what a tester reported.
 */
import { test, expect, type Page } from '@playwright/test';

/** The chord, spelled once. Playwright maps Meta to Command on macOS and to
 * the Windows key elsewhere; the handler accepts either meta or ctrl, so the
 * platform-correct modifier is what gets pressed. */
const SETTINGS_CHORD = process.platform === 'darwin' ? 'Meta+Comma' : 'Control+Comma';

const settingsDialog = (page: Page) => page.getByRole('dialog', { name: 'Settings' });
const setupDialog = (page: Page) => page.getByRole('dialog', { name: 'First-run setup' });
const runSetupButton = (page: Page) => page.getByRole('button', { name: 'Run setup', exact: true });

/** The route the overlay is drawn over. Spelled once here, and it must match
 * SETUP_HOST_ROUTE in $lib/setup/run-setup. The performance route writes its
 * own deep-link query (`?playlist=all`, performance-deeplink.ts since commit d1d6dcaee)
 * via replaceState once it boots, so the path is pinned and the query is not. */
const HOST_ROUTE = /\/performance\/?(?:\?[^#]*)?$/;

/** The three ways forward the detect step must ALWAYS offer. */
const ESCAPE_LABELS = ['Look again', 'Choose a folder instead', 'Continue without importing'];

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

/**
 * The wizard is on screen as an OVERLAY over the app.
 *
 * Not a URL assertion about /setup any more: /setup is a door that opens the
 * dialog and hands the browser on to the host route, so asserting the old URL
 * would pin the redirect's transient middle rather than the outcome.
 */
async function expectWizard(page: Page): Promise<void> {
	const dialog = setupDialog(page);
	await expect(dialog).toBeVisible();
	await expect(page).toHaveURL(HOST_ROUTE);
	await expect(dialog.getByRole('heading', { name: 'First-run setup' })).toBeVisible();
	await expect(dialog.locator('.steps .step').first()).toContainText('Welcome');
	await expect(page.getByText(PROBE_RACE)).toHaveCount(0);
	// The app is BEHIND it, not replaced by it. .perf-root is the performance
	// view's own root; an overlay that covered the app would have no reason to
	// be an overlay.
	await expect(page.locator('.perf-root').first()).toBeVisible();
}

/** What detection genuinely reports on the engine behind THIS run. No stub:
 * the webkit config sandboxes HOME so rekordbox really is absent, and the
 * vite config talks to a lane engine where it really is present. */
async function fatalBlockers(page: Page): Promise<string[]> {
	const body = await page.evaluate(async () => {
		const response = await fetch('/api/v1/setup/detect/rekordbox');
		return (await response.json()) as { blockers?: string[] };
	});
	return (body.blockers ?? []).filter((code) => code !== 'rekordbox_share_missing');
}

test.describe('setup entry points', () => {
	test('Cmd+, opens settings, and Run setup lands on the wizard', async ({ page }) => {
		// No rb-meta allowlist here on purpose. This suite's library is entirely
		// locally imported, so every listing row reports has_rb_mapping false and
		// the browser issues no rb-meta request at all. The 404 that used to be
		// filtered out here is doubly gone: #505 made the endpoint answer 200 for
		// such a track, and the flag stops the request being made. A /rb-meta
		// console error reappearing is a real regression the gate should catch.
		const errors: string[] = [];
		page.on('console', (msg) => {
			if (msg.type() !== 'error') return;
			errors.push(`${msg.text()} [${msg.location()?.url ?? ''}]`);
		});

		// BuildIdentity issues this on first navigation; register before goto so
		// the initial check cannot race past the test.
		const updateCheckResponsePromise = page.waitForResponse((response) =>
			response.url().includes('/api/v1/update/check')
		);

		await gotoShellReady(page, '/');
		await expect(settingsDialog(page)).toHaveCount(0);

		await page.keyboard.press(SETTINGS_CHORD);
		await expect(settingsDialog(page)).toBeVisible();

		await runSetupButton(page).click();
		await expectWizard(page);

		// BuildIdentity's update check must have been issued and answered by the
		// engine with the endpoint it is configured to read. The manifest behind
		// that endpoint lives on github.com and is validated in its own test
		// below, so an outage there cannot discard this test's local evidence.
		const updateCheckBody = (await (await updateCheckResponsePromise).json()) as {
			endpoint: string;
		};
		expect(updateCheckBody.endpoint).toMatch(/^https:\/\//);

		// The USB panel's 503 is a DESIGNED refusal, not a fault, and it is the
		// one console error this page can legitimately emit on a CI host.
		// LibraryNav mounts on '/' and polls GET /api/v1/usb/volumes every 5s
		// (startUsbWatch, added by e474e66a2 LIBMX-13, Sun 13 Sep 2026); that
		// route answers 503 `unsupported_platform:<os>` on anything that is not
		// darwin (df1987077, Sun 31 Aug 2026 - it refuses rather than returning
		// an empty list that would read as "nothing is plugged in"). The CI
		// runner is Linux, so 503 is the CORRECT answer there and WebKit logs
		// the refused fetch itself; the app handles it (usbTracker.lastError)
		// and logs nothing of its own.
		//
		// The allowance is deliberately pinned to that one endpoint AND that
		// one status, so a usb/volumes 500, any other endpoint's failure, and
		// any app-emitted console error all still fail this assertion.
		const usbCapabilityRefusal = /status of 503 .*\/api\/v1\/usb\/volumes/;
		expect(
			errors.filter((e) => !e.includes('favicon') && !usbCapabilityRefusal.test(e))
		).toEqual([]);
	});

	test('the release manifest the engine names is reachable and well-formed', async ({ request }) => {
		// Separate from the setup-flow test above on purpose (Codex P2 on #3732):
		// the endpoint is the release manifest on github.com, not this engine, so
		// a 5xx here is the CDN or the runner's egress failing to answer, which
		// says nothing about the build under test. Retry a few times, then report
		// UNMEASURED (a skip naming the status) rather than a red verdict, without
		// taking any local assertion down with it. A 200 is asserted in full and a
		// 404 (a missing manifest) still fails.
		const updateCheck = await request.get('/api/v1/update/check');
		expect(updateCheck.status()).toBe(200);
		const { endpoint } = (await updateCheck.json()) as { endpoint: string };
		let manifestResponse = await request.get(endpoint);
		for (let attempt = 1; attempt < 4 && manifestResponse.status() >= 500; attempt += 1) {
			await new Promise((resolve) => setTimeout(resolve, 2_000 * attempt));
			manifestResponse = await request.get(endpoint);
		}
		test.skip(
			manifestResponse.status() >= 500,
			`UNMEASURED: release manifest ${endpoint} answered ${manifestResponse.status()} after 4 attempts`
		);
		expect(manifestResponse.status()).toBe(200);
		const manifest = (await manifestResponse.json()) as {
			version?: string;
			platforms?: Record<string, { url?: string; signature?: string }>;
		};
		expect(typeof manifest.version).toBe('string');
		expect((manifest.version ?? '').length).toBeGreaterThan(0);
		expect(manifest.platforms).toBeTruthy();
		const darwinEntry = manifest.platforms?.['darwin-aarch64'];
		expect(typeof darwinEntry?.url).toBe('string');
		expect((darwinEntry?.url ?? '').length).toBeGreaterThan(0);
		expect(typeof darwinEntry?.signature).toBe('string');
		expect((darwinEntry?.signature ?? '').length).toBeGreaterThan(0);
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

	test('the admin Diagnostics tab is visible but not selected by default', async ({ page }) => {
		await gotoShellReady(page, '/admin');
		const tabs = page.getByRole('tablist', { name: 'admin sections' });
		await expect(tabs.getByRole('tab', { name: 'KPI ledger' })).toHaveAttribute(
			'aria-selected',
			'true'
		);
		const diagnostics = tabs.getByRole('tab', { name: 'Diagnostics' });
		await expect(diagnostics).toBeVisible();
		await expect(diagnostics).toHaveAttribute('aria-selected', 'false');
		const playground = tabs.getByRole('tab', { name: 'Playground' });
		await expect(playground).toBeVisible();
		await expect(playground).toHaveAttribute('aria-selected', 'false');
	});

	test('the admin Playground tab shows API and SQL consoles', async ({ page }) => {
		await gotoShellReady(page, '/admin?tab=playground');
		const tabs = page.getByRole('tablist', { name: 'admin sections' });
		await expect(tabs.getByRole('tab', { name: 'Playground' })).toHaveAttribute(
			'aria-selected',
			'true'
		);
		await expect(tabs.getByRole('tab', { name: 'KPI ledger' })).toHaveAttribute(
			'aria-selected',
			'false'
		);
		await expect(page.getByRole('heading', { name: 'API console' })).toBeVisible();
		await expect(page.getByRole('heading', { name: 'SQL console' })).toBeVisible();
	});

	test('the admin Diagnostics tab shows diagnostics sections', async ({ page }) => {
		await gotoShellReady(page, '/admin?tab=diagnostics');
		const tabs = page.getByRole('tablist', { name: 'admin sections' });
		await expect(tabs.getByRole('tab', { name: 'Diagnostics' })).toHaveAttribute(
			'aria-selected',
			'true'
		);
		await expect(tabs.getByRole('tab', { name: 'KPI ledger' })).toHaveAttribute(
			'aria-selected',
			'false'
		);
		await expect(page.getByRole('heading', { name: 'Capability probe' })).toBeVisible();
	});

	test('/setup deep-links into the overlay instead of 404ing', async ({ page }) => {
		// Bookmarks, SETUP_ROUTE and "open /setup" agent instructions all still
		// exist. They must land on the wizard, not on a dead route and not on a
		// second copy of it.
		await gotoShellReady(page, '/setup');
		await expectWizard(page);
		// One wizard on screen, not two.
		await expect(page.getByRole('heading', { name: 'First-run setup' })).toHaveCount(1);
	});

	test('the detect step always offers three enabled ways forward', async ({ page }) => {
		// THE regression this suite exists for: a tester met a detect step whose
		// only enabled control was one he did not recognise as an escape. None
		// of these three is gated on what detection found.
		await gotoShellReady(page, '/setup');
		const dialog = setupDialog(page);
		await expect(dialog).toBeVisible();
		await dialog.getByRole('button', { name: 'Get started' }).click();

		for (const label of ESCAPE_LABELS) {
			const button = dialog.getByRole('button', { name: label, exact: true });
			await expect(button, `${label} must be on screen`).toBeVisible();
			await expect(button, `${label} must never be disabled`).toBeEnabled();
			// House rule: a control says what it does and what it will change.
			await expect(button).toHaveAttribute('title', /\/api\/v1\/setup\//);
		}
	});

	test('detection reports itself honestly, in the right colour', async ({ page }) => {
		// Real data both ways. Under the webkit artifact config the engine's
		// HOME is a sandbox, so rekordbox genuinely is not there; under vite it
		// is the lane engine on a machine that has it.
		await gotoShellReady(page, '/setup');
		const dialog = setupDialog(page);
		await expect(dialog).toBeVisible();
		await dialog.getByRole('button', { name: 'Get started' }).click();
		await dialog.getByRole('radio', { name: 'A rekordbox collection on this machine' }).check();

		// The scanning state must resolve into a verdict, never stick.
		await expect(dialog.locator('.probes li').first()).toBeVisible();

		const fatal = await fatalBlockers(page);
		const continueButton = dialog.getByRole('button', { name: 'Continue', exact: true });

		if (fatal.length === 0) {
			await expect(continueButton).toBeEnabled();
			await expect(dialog.locator('.why')).toHaveCount(0);
			return;
		}

		// A fatal blocker: red, announced, and the reason is ON SCREEN rather
		// than hidden in a hover title.
		const alert = dialog.locator('p.fatal[role="alert"]').first();
		await expect(alert).toBeVisible();
		await expect(alert).toHaveCSS('color', 'rgb(255, 90, 90)');
		await expect(continueButton).toBeDisabled();
		const why = dialog.locator('.why');
		await expect(why).toBeVisible();
		await expect(why).toContainText('Continue is not available');
		await expect(why).toContainText('Use one of the three options above instead');
		// The probe line that is the REASON is red too, not plain prose.
		await expect(dialog.locator('.probes li.danger').first()).toHaveCSS(
			'color',
			'rgb(255, 90, 90)'
		);
	});

	test('the overlay minimises to a chip and comes back', async ({ page }) => {
		// A 10,000-track import must not hold the whole screen hostage, and the
		// chip must reopen the SAME wizard rather than restart it.
		await gotoShellReady(page, '/setup');
		const dialog = setupDialog(page);
		await expect(dialog).toBeVisible();
		await dialog.getByRole('button', { name: 'Get started' }).click();
		await expect(dialog.locator('.steps .step.current')).toContainText('Find your music');

		await dialog.getByRole('button', { name: 'Minimise' }).click();
		await expect(setupDialog(page)).toHaveCount(0);
		const chip = page.locator('.su-chip');
		await expect(chip).toBeVisible();
		// The performance UI is fully usable behind the chip.
		await expect(page.locator('.perf-root').first()).toBeVisible();

		await chip.click();
		await expect(setupDialog(page)).toBeVisible();
		// Same step, not a restart.
		await expect(setupDialog(page).locator('.steps .step.current')).toContainText(
			'Find your music'
		);
	});

	test('STANDALONE-08: rekordbox detection alone does not opt in or import', async ({
		page
	}) => {
		// Mutation guard: reverting the initial source to rekordbox must fail here.
		const importPosts: string[] = [];
		page.on('request', (request) => {
			if (request.method() === 'POST' && request.url().includes('/api/v1/setup/import')) {
				importPosts.push(request.url());
			}
		});

		await gotoShellReady(page, '/setup');
		const dialog = setupDialog(page);
		await expect(dialog).toBeVisible();
		await dialog.getByRole('button', { name: 'Get started' }).click();

		const rekordboxRadio = dialog.getByRole('radio', {
			name: 'A rekordbox collection on this machine'
		});
		const folderRadio = dialog.getByRole('radio', { name: /folder of audio files/ });
		await expect(rekordboxRadio).not.toBeChecked();
		await expect(folderRadio).not.toBeChecked();
		await expect(importPosts).toEqual([]);

		const continueButton = dialog.getByRole('button', { name: 'Continue', exact: true });
		await expect(continueButton).toBeDisabled();
		await expect(dialog.locator('.why')).toContainText('choose an import source');

		await rekordboxRadio.check();
		await expect(rekordboxRadio).toBeChecked();
		await expect(dialog.locator('.probes li').first()).toBeVisible();
		await expect(importPosts).toEqual([]);
		await expect(dialog.locator('.steps .step.current')).toContainText('Find your music');

		const fatal = await fatalBlockers(page);
		if (fatal.length === 0) {
			await expect(continueButton).toBeEnabled();
			await continueButton.click();
			await expect(dialog.locator('.steps .step.current')).toContainText('Confirm the import');
		}
	});

	test('STANDALONE-08: declining import completes setup and is not re-offered', async ({
		page
	}) => {
		// Mutation guard: making dismissed-empty libraries reopen must fail here.
		await gotoShellReady(page, '/setup');
		const dialog = setupDialog(page);
		await expect(dialog).toBeVisible();
		await dialog.getByRole('button', { name: 'Get started' }).click();
		await dialog
			.getByRole('button', { name: 'Continue without importing', exact: true })
			.click();

		await expect(setupDialog(page)).toHaveCount(0);
		await expect(page.locator('.perf-root').first()).toBeVisible();

		const statusAfterDismiss = await page.evaluate(async () => {
			const response = await fetch('/api/v1/setup/status');
			return (await response.json()) as { dismissed: boolean; should_show_wizard: boolean };
		});
		expect(statusAfterDismiss.dismissed).toBe(true);
		expect(statusAfterDismiss.should_show_wizard).toBe(false);

		await page.reload();
		await page.waitForFunction(
			() => typeof (window as unknown as { __mdtPerfLog?: unknown }).__mdtPerfLog === 'function',
			undefined,
			{ timeout: 30_000 }
		);
		await expect(setupDialog(page)).toHaveCount(0);

		// Re-arm for the next test.
		await page.keyboard.press(SETTINGS_CHORD);
		await runSetupButton(page).click();
		await expectWizard(page);
	});

	test('continuing without importing closes into an honest empty state', async ({ page }) => {
		// the maintainer's directive: a next step must ALWAYS be available. The last
		// resort is leaving, and leaving must not be silent.
		await gotoShellReady(page, '/setup');
		const dialog = setupDialog(page);
		await expect(dialog).toBeVisible();
		await dialog.getByRole('button', { name: 'Get started' }).click();
		await dialog
			.getByRole('button', { name: 'Continue without importing', exact: true })
			.click();

		await expect(setupDialog(page)).toHaveCount(0);
		await expect(page.locator('.perf-root').first()).toBeVisible();

		const dismissed = await page.evaluate(async () => {
			const response = await fetch('/api/v1/setup/status');
			return ((await response.json()) as { dismissed: boolean }).dismissed;
		});
		expect(dismissed, 'the dismissal is engine-side, not a tab-local flag').toBe(true);

		// Re-openable, always: put it back so the next test starts clean.
		await page.keyboard.press(SETTINGS_CHORD);
		await runSetupButton(page).click();
		await expectWizard(page);
	});

	test('the folder path placeholder reads as a hint, not a pre-filled value', async ({
		page
	}) => {
		await gotoShellReady(page, '/setup');
		const dialog = setupDialog(page);
		await expect(dialog).toBeVisible();
		await dialog.getByRole('button', { name: 'Get started' }).click();
		await dialog
			.getByRole('button', { name: 'Choose a folder instead', exact: true })
			.click();

		const folderInput = dialog.getByRole('textbox', { name: 'Folder to import' });
		await expect(folderInput).toBeVisible();
		await expect(folderInput).toHaveValue('');

		const placeholderStyle = await folderInput.evaluate((el) => {
			const style = window.getComputedStyle(el, '::placeholder');
			return {
				fontStyle: style.fontStyle,
				opacity: Number.parseFloat(style.opacity),
				color: style.color
			};
		});
		expect(placeholderStyle.fontStyle).toBe('italic');
		expect(placeholderStyle.opacity).toBeLessThan(1);

		const emptyScreenshot = await folderInput.screenshot();

		await folderInput.fill('/Users/you/Music');
		await folderInput.blur();
		await expect(folderInput).toHaveValue('/Users/you/Music');

		const typedStyle = await folderInput.evaluate((el) => {
			const style = window.getComputedStyle(el);
			return {
				fontStyle: style.fontStyle,
				opacity: Number.parseFloat(style.opacity),
				color: style.color
			};
		});
		expect(typedStyle.fontStyle).not.toBe('italic');
		expect(typedStyle.opacity).toBe(1);

		// The value lands before the glyphs do. A single screenshot here catches
		// whatever frame the compositor happened to have up, which on a loaded
		// runner is still the empty one -- so wait for the typed state to PAINT
		// differently rather than asserting against one arbitrary frame.
		await expect
			.poll(async () => (await folderInput.screenshot()).equals(emptyScreenshot), {
				message: 'the typed value must render differently from the italic placeholder'
			})
			.toBe(false);
	});

	test('the build identity chip states this app address in its foldout', async ({ page, context }) => {
		// The reason the chip moved into the tray at all: a tester could not
		// find the packaged app's URL, because the engine binds an ephemeral
		// port and nothing on screen said which one.
		await gotoShellReady(page, '/');
		const chip = page.locator('.build-identity');
		await expect(chip).toBeVisible();
		await chip.getByRole('button').first().click();
		const urlLink = chip.locator('a.url');
		await expect(urlLink).toBeVisible();
		await expect(urlLink).toHaveAttribute('href', /^https?:\/\//);
		await expect(chip.getByRole('button', { name: 'copy all details' })).toBeVisible();
		await context.grantPermissions(['clipboard-read', 'clipboard-write']);
		await chip.getByRole('button', { name: 'copy all details' }).click();
		await expect
			.poll(async () => page.evaluate(() => navigator.clipboard.readText()))
			.toMatch(/git_sha:/);
	});
});
