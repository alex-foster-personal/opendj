import { expect, test, type Page } from '@playwright/test';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { fixtureManifestExists, primaryFixtureStableId } from './support/fixture-manifest';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const MANIFEST_PATH = join(
	process.env.MDT_DATA_DIR ?? join(REPOSITORY_ROOT, 'data'),
	'fixture-manifest.json'
);
const HAS_MANIFEST = fixtureManifestExists(MANIFEST_PATH);
const FIXTURE_STABLE_ID = HAS_MANIFEST ? primaryFixtureStableId(MANIFEST_PATH) : '';

const PRIORITY_CONTROLS = [
	'button[title^="BeatSyncMax"]',
	'span.ap-wrap > button[title^="AutoPlay"]',
	'button[aria-label="Jobs drawer"]',
	'[role="slider"][aria-label="master volume"]'
];

async function _hitTarget(page: Page, selector: string): Promise<{ hit: boolean; description: string }> {
	return page.locator(selector).evaluate((element) => {
		const rect = element.getBoundingClientRect();
		const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
		return {
			hit: hit === element || element.contains(hit),
			description: hit instanceof Element ? `${hit.tagName}.${hit.className}` : String(hit)
		};
	});
}

test('responsive TopBar keeps priority controls reachable and reports the two-track contract', async ({ page }) => {
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	for (const viewport of [
		{ width: 1864, height: 947 },
		// 1366px: a very common laptop width, and inside the band between the
		// 1400px and 1340px topbar breakpoints (pin T3, packet 9h) that had no
		// coverage until this line - both 1280px and 1366px are asserted here
		// so neither breakpoint's neighbourhood can silently regress again.
		{ width: 1366, height: 768 },
		{ width: 1280, height: 800 },
		{ width: 1024, height: 800 },
		{ width: 820, height: 800 }
	]) {
		await page.setViewportSize(viewport);
		for (const selector of PRIORITY_CONTROLS) {
			await expect(page.locator(selector)).toBeVisible();
			await expect
				.poll(async () => (await _hitTarget(page, selector)).hit, {
					message: `${viewport.width}px ${selector} did not settle on its own hit target`
				})
				.toBe(true);
		}
		if (viewport.width > 820) {
			await expect(page.locator('input[aria-label="text command entry"]')).toBeVisible();
			await expect
				.poll(async () => (await _hitTarget(page, 'input[aria-label="text command entry"]')).hit, {
					message: `${viewport.width}px command entry did not settle on its own hit target`
				})
				.toBe(true);
		}
	}

	await page.locator('span.ap-wrap > button[title^="AutoPlay"]').focus();
	await expect(page.locator('.ap-menu')).toBeVisible();
	await expect(page.locator('button.ap-two-track')).toBeDisabled();
	await expect(page.locator('button.ap-two-track')).toHaveAttribute('title', /Not built yet/);

	const refusal = await page.evaluate(async () => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		try {
			await ipc.dispatch({ type: 'auto_play_two_track' });
			return null;
		} catch (error) {
			return error instanceof Error ? error.message : String(error);
		}
	});
	expect(refusal).toMatch(/not_implemented/);
});

test('compact 800x600 performance top bar shows Stage, compact lyric cue, and cold-load Stage wiring', async ({
	page
}, testInfo) => {
	await page.setViewportSize({ width: 800, height: 600 });
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	const stageButton = page.getByRole('button', { name: 'Open karaoke stage' });
	const lyrButton = page.locator('button.topbar-slot-lyr');
	const compactChip = page.locator('.lyrics-compact-chip');

	await expect(stageButton).toBeVisible();
	await expect(stageButton).toBeDisabled();
	await expect(lyrButton).toHaveAttribute('aria-pressed', 'true');
	await expect(compactChip).toBeVisible();

	await expect
		.poll(async () => (await _hitTarget(page, 'button.topbar-slot-stage')).hit, {
			message: '800x600 Stage control must be hittable at its own center'
		})
		.toBe(true);

	const screenshot = await page.screenshot({ fullPage: false });
	await testInfo.attach('performance-800x600-stage-lyrics', {
		body: screenshot,
		contentType: 'image/png'
	});
});

/**
 * The sign-in control is the one thing in this row with no other door on this
 * route (issue #2357): the shell topbar that carries the account bauble
 * everywhere else is not rendered on /performance. It used to be
 * `display: none` below 825px - the harden-lane capture width - so the route
 * had no sign-in affordance at all, and above that width it was a bare circle
 * whose words lived only in aria-label.
 *
 * Two claims, both measured here rather than in CSS text:
 *  1. the control is visible, labelled and hittable at every width, and
 *  2. the row does not OVERFLOW while it is. That second one is the whole
 *     difficulty: `.rb-topbar` is `overflow: hidden`, so a row that does not
 *     fit silently clips its right-hand end - which is where this control
 *     lives. A label that fits by pushing the row past its own width would
 *     pass a visibility check and still be cut in half on screen.
 */
const SIGN_IN_WIDTHS = [800, 820, 1024, 1280, 1366, 1440, 1920];

test('the performance top bar keeps a labelled, hittable sign-in control at every width', async ({
	page
}) => {
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	// What the daemon says about its own OAuth client decides which face the
	// control wears; the test asserts the control AGREES with it either way.
	const configured = await page.evaluate(async () => {
		const response = await fetch('/api/v1/health');
		const body = (await response.json()) as { google_oauth_configured?: unknown };
		if (typeof body.google_oauth_configured !== 'boolean') {
			throw new Error('health did not answer google_oauth_configured as a boolean');
		}
		return body.google_oauth_configured;
	});
	const expectedLabel = configured ? 'Sign in with Google' : 'Sign-in unavailable';

	for (const width of SIGN_IN_WIDTHS) {
		await page.setViewportSize({ width, height: width === 800 ? 600 : 800 });
		const button = page.locator('.bauble-root .bauble');
		await expect(button).toBeVisible();
		if (configured) await expect(button).toBeEnabled();
		else await expect(button).toBeDisabled();
		await expect(page.locator('.bauble-root .bauble-label')).toHaveText(expectedLabel);
		await expect
			.poll(async () => (await _hitTarget(page, '.bauble-root .bauble')).hit, {
				message: `${width}px sign-in control did not settle on its own hit target`
			})
			.toBe(true);
		// The bail-out: a row that is wider than its own box clips this control
		// (it is the right-most child) with no console error and no scrollbar.
		const overflow = await page.locator('header.rb-topbar').evaluate((bar) => ({
			overflow: bar.scrollWidth - bar.clientWidth,
			right: bar.getBoundingClientRect().right
		}));
		expect(
			overflow.overflow,
			`${width}px: the top bar overflows by ${overflow.overflow}px, so its right-hand ` +
				`end (the sign-in control) is clipped`
		).toBeLessThanOrEqual(0);
		const rect = await button.evaluate((element) => {
			const r = element.getBoundingClientRect();
			return { right: r.right, left: r.left, width: r.width };
		});
		expect(rect.right, `${width}px: sign-in control runs past the viewport`).toBeLessThanOrEqual(
			width
		);
		expect(rect.left).toBeGreaterThanOrEqual(0);
		expect(rect.width).toBeGreaterThan(0);
	}
});

// requirement: PERF-UI-06
// [if] /performance is at a width where the command entry is offered [then]
// its centre passes a real elementFromPoint hit test and the top bar does not
// overflow horizontally, [else stop].
const CMD_ENTRY_WIDTHS = [1400, 1440, 1530, 1535, 1740, 1745, 1920] as const;

test('the performance top bar keeps the command entry hittable across the 1400px-plus ladder', async ({
	page
}) => {
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	const commandInput = page.locator('input[aria-label="text command entry"]');

	for (const width of CMD_ENTRY_WIDTHS) {
		await page.setViewportSize({ width, height: 800 });
		await expect(commandInput).toBeVisible();
		await expect
			.poll(async () => (await _hitTarget(page, 'input[aria-label="text command entry"]')).hit, {
				message: `${width}px command entry did not settle on its own hit target`
			})
			.toBe(true);
		const overflow = await page.locator('header.rb-topbar').evaluate(
			(bar) => bar.scrollWidth - bar.clientWidth
		);
		expect(
			overflow,
			`${width}px: the top bar overflows by ${overflow}px while the command entry is offered`
		).toBeLessThanOrEqual(0);
	}
});

test('loaded deck Stage opens the performance karaoke overlay and closes cleanly', async ({ page }) => {
	test.skip(
		!HAS_MANIFEST,
		'fixture manifest missing - the loaded-deck Stage interaction needs the generated performance fixture'
	);

	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await page.evaluate(async (stableId) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		await ipc.dispatch({ type: 'load', deck: 1, stable_id: stableId });
	}, FIXTURE_STABLE_ID);
	await page.waitForFunction(() => {
		const ipc = window.musicDjToolsPerformance;
		return ipc?.query().decks[1]?.stable_id !== null;
	});

	const stageButton = page.getByRole('button', { name: 'Open karaoke stage' });
	await expect(stageButton).toBeEnabled();
	await stageButton.click();

	const overlay = page.locator('.stage[role="dialog"]');
	await expect(overlay).toBeVisible();
	await expect(overlay).toContainText('DECK 1');

	await page.keyboard.press('Escape');
	await expect(overlay).toHaveCount(0);
});
