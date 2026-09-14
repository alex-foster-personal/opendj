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
