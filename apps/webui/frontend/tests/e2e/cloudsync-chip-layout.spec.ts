import { expect, test, type Locator, type Page } from '@playwright/test';

import { spendBootLanding } from './support/boot-landing';

test.setTimeout(120_000);

// `/` is Library mode's full-window Crate Desk (LIBWS-01), which has no app
// shell; /pairings is a plain app-shell route that still mounts this header.
const APP_SHELL_ROUTE = '/pairings';
const COMPACT_VIEWPORT = { width: 900, height: 700 };

// The chip under test lives in the APP-SHELL header, and both tests open `/`
// cold: without this the PERFMODE-11 landing redirect can detach the chip
// mid-click. support/boot-landing.ts has the full account.
test.beforeEach(async ({ page }) => {
	await spendBootLanding(page);
});

/** Minimal GET /api/v1/cloudsync/status body with a live scheduler heartbeat. */
function runningStatus(over: Record<string, unknown> = {}): Record<string, unknown> {
	return {
		enabled: true,
		configured: true,
		running: true,
		heartbeat_at: new Date().toISOString(),
		enabled_source: 'file',
		endpoint_source: 'file',
		reason: null,
		signed_in_as: null,
		last_push_at: '2026-11-13T00:00:00.000000+00:00',
		last_pull_at: '2026-11-13T00:00:00.000000+00:00',
		last_result: { status: 'ok', message: 'pushed 0, pulled 0' },
		rows_pending: 0,
		hash_pending: 0,
		quarantined: 0,
		excluded_total: 0,
		endpoint: 'http://box.example-tailnet.ts.net:8686',
		recent_results: [],
		update_required: null,
		...over
	};
}

async function stubCloudSyncStatus(page: Page, body: Record<string, unknown>): Promise<void> {
	await page.route('**/api/v1/cloudsync/status', (route) => route.fulfill({ json: body }));
}

async function expectCompactSingleLine(chip: Locator): Promise<void> {
	const box = await chip.boundingBox();
	expect(box).not.toBeNull();
	expect(box!.height).toBeLessThanOrEqual(24);
	expect(box!.width).toBeGreaterThan(box!.height);
}

// requirement: CSSTATUS-04
// [if] the CloudSync chip is healthy on first load [then] it is not in error state and keeps its quick-actions title, [else stop]
test('at 900px the CloudSync chip is a single-line quick-actions trigger', async ({ page }) => {
	await page.setViewportSize(COMPACT_VIEWPORT);
	await page.goto(APP_SHELL_ROUTE);

	const chip = page.getByTestId('cloudsync-status-chip');
	await expect(chip).toBeVisible({ timeout: 20_000 });
	await expect(chip.locator('.chip-label-short')).toHaveText('off');
	await expect(chip.locator('.chip-label-full')).toBeHidden();

	await expectCompactSingleLine(chip);

	await expect(chip).toHaveAttribute('title', /CloudSync/);
	await expect(chip).toHaveAttribute('title', /Click for quick actions\./);
	await expect(chip).not.toHaveClass(/error/);
	await expect(chip.locator('.chip-label-short')).not.toHaveText('err');

	await chip.click();
	await expect(page.getByTestId('cloudsync-quick-actions-popover')).toBeVisible();
	await expect(page).not.toHaveURL(/\/cloudsync/);
});

test('at 1280px the CloudSync chip shows the full label in one line', async ({ page }) => {
	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto(APP_SHELL_ROUTE);

	const chip = page.getByTestId('cloudsync-status-chip');
	await expect(chip).toBeVisible({ timeout: 20_000 });
	await expect(chip.locator('.chip-label-full')).toHaveText('sync: off');
	await expect(chip.locator('.chip-label-short')).toBeHidden();

	await expectCompactSingleLine(chip);
});

// requirement: CSSTATUS-13867
// [if] viewport is 1024 px or narrower and chip state is syncing, ok, error, or inconclusive [then] compact layout holds, [else stop]
test('at 900px stubbed CloudSync states stay on one compact line', async ({ page }) => {
	await page.setViewportSize(COMPACT_VIEWPORT);

	const cases = [
		{
			name: 'syncing',
			status: runningStatus({ last_result: null }),
			assert: async (chip: Locator) => {
				await expect(chip.locator('.chip-label-short')).toHaveText('sync');
			}
		},
		{
			name: 'ok',
			status: runningStatus({ last_result: { status: 'ok', message: 'pushed 1, pulled 1' } }),
			assert: async (chip: Locator) => {
				await expect(chip).toHaveClass(/ok/);
				await expect(chip.getByTestId('cloudsync-chip-tick')).toBeVisible();
				await expect(chip.locator('.chip-label-short')).toBeHidden();
			}
		},
		{
			name: 'error',
			status: runningStatus({ last_result: { status: 'error', message: 'hub refused' } }),
			assert: async (chip: Locator) => {
				await expect(chip).toHaveClass(/error/);
				await expect(chip.locator('.chip-label-short')).toHaveText('err');
			}
		},
		{
			name: 'inconclusive',
			status: runningStatus({
				last_result: { status: 'inconclusive', message: 'digest excluded rows' }
			}),
			assert: async (chip: Locator) => {
				await expect(chip).toHaveClass(/inconclusive/);
				await expect(chip.locator('.chip-label-short')).toHaveText('inc');
			}
		}
	] as const;

	for (const { status, assert } of cases) {
		await page.unrouteAll({ behavior: 'ignoreErrors' });
		await stubCloudSyncStatus(page, status);
		await page.goto(APP_SHELL_ROUTE);

		const chip = page.getByTestId('cloudsync-status-chip');
		await expect(chip).toBeVisible({ timeout: 20_000 });
		await expect(chip.locator('.chip-label-full')).toBeHidden();
		await assert(chip);
		await expectCompactSingleLine(chip);
	}
});
