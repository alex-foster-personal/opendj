import { expect, test } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const SCREENSHOT_DIR = join(REPOSITORY_ROOT, '.tmp');

test('real event-store analytics render and filter through the HTTP contract', async ({ page }) => {
	const consoleErrors: string[] = [];
	const failedResources: { url: string; status: number }[] = [];
	page.on('console', (message) => {
		if (message.type() === 'error') consoleErrors.push(message.text());
	});
	page.on('response', (response) => {
		if (response.status() < 400) return;
		failedResources.push({ url: response.url(), status: response.status() });
	});

	const initialResponse = page.waitForResponse(
		(response) => response.url().includes('/api/play-analytics?') && response.status() === 200
	);
	// No private 15 s bound: the check is issued from BuildIdentity's onMount,
	// which under a saturated runner host (nightly 34311277173, Wed 9 Sep 2026,
	// nine e2e jobs on sixteen threads) landed later than that while the
	// analytics response itself had already arrived. The test's own timeout
	// bounds it; a missing check still fails, only not ahead of the page.
	const updateCheck = page.waitForResponse((response) =>
		response.url().includes('/api/v1/update/check')
	);
	await page.goto('/play-analytics');
	await initialResponse;

	await expect(page.getByRole('heading', { name: 'Play analytics' })).toBeVisible();
	await expect(page.getByLabel('Play summary')).toContainText(
		/2\s*sessions\s*4\s*plays\s*3\s*unique tracks/
	);
	await expect(page.getByRole('heading', { name: 'Recent sessions' })).toBeVisible();
	await expect(page.getByText('warehouse-2026-07-21')).toBeVisible();
	await expect(page.getByRole('heading', { name: 'Most played' })).toBeVisible();
	await expect(page.getByText('Alpha', { exact: true })).toBeVisible();

	mkdirSync(SCREENSHOT_DIR, { recursive: true });
	await page.screenshot({ path: join(SCREENSHOT_DIR, 'play-analytics-1280x800.png') });

	const filteredResponse = page.waitForResponse(
		(response) =>
			response.url().includes('share_state=shared_local') && response.status() === 200
	);
	await page.getByLabel('Session visibility').selectOption('shared_local');
	await filteredResponse;
	await expect(page.getByLabel('Play summary')).toContainText(
		/1\s*sessions\s*2\s*plays\s*2\s*unique tracks/
	);
	await expect(page.getByText('warehouse-2026-07-21')).not.toBeVisible();
	await expect(page.getByText('studio-2026-07-20')).toBeVisible();
	// /update/check faults 502 identity-unavailable in every unbuilt engine
	// (no OPENDJ_PAYLOAD_MANIFEST -> no app_version to compare), the same fault
	// setup-entry-points.spec.ts already carves out against the real route.
	// Scoped to this endpoint AND this status: removal, mis-mounting, or any
	// other status from this route still fails the gate.
	expect(
		failedResources.every(
			({ url, status }) => url.endsWith('/favicon.svg') || (url.includes('/update/check') && status === 502)
		)
	).toBe(true);
	const updateCheckResponse = await updateCheck;
	expect(updateCheckResponse.status()).toBe(502);
	expect((await updateCheckResponse.json()).status).toBe('identity-unavailable');
	expect(
		consoleErrors.filter((error) => !error.startsWith('Failed to load resource:'))
	).toEqual([]);
});
