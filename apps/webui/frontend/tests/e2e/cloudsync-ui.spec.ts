/**
 * /cloudsync Status, Config, Sync now and Fleet (plan W17), in a real
 * browser against a real hub and a real spoke (playwright.cloudsync-ui.config.ts).
 *
 * Single-line acceptance checks:
 * - if the chip reads anything but 'sync: off' with no fresh heartbeat -> broken.
 * - if saving the config form does not persist through GET /cloudsync/config -> broken.
 * - if Sync now does not reach the hub (the hub never learns the spoke's
 *   machine name) or no journal row appears -> broken.
 * - if the Fleet tab does not list this machine -> broken.
 * - if enabling does not bring a fresh heartbeat (Running: yes) and light the chip -> broken.
 * - if the chip needs a reload to leave 'off' once the heartbeat is fresh -> broken.
 * - if a cell with no stored policy shows anything but 'unset', or its budget is live -> broken.
 * - if a real declared 409 does not surface a plain conflict summary with raw text only in
 *   Technical details -> broken.
 */
import { expect, test, type Page } from '@playwright/test';

import { CLOUDSYNC_UI_HUB_URL, CLOUDSYNC_UI_ORIGIN } from './playwright.cloudsync-ui.config';

const SPOKE_NAME = 'e2e-cloudsync-spoke';

async function chip(page: Page) {
	return page.getByRole('button', { name: 'CloudSync status' });
}

test('config, Sync now, journal row, fleet, then a live heartbeat lights the chip', async ({
	page,
	request
}) => {
	// Any 5xx from the spoke fails the run: a page that renders while a
	// background call (machines self-registration, overview) errors is not a
	// passing page. This is what caught the machines.name collision.
	const serverErrors: string[] = [];
	page.on('response', (r) => {
		if (r.status() >= 500) serverErrors.push(`${r.status()} ${r.request().method()} ${r.url()}`);
	});

	await page.goto(`${CLOUDSYNC_UI_ORIGIN}/cloudsync`);

	// 1. Nothing configured, no heartbeat: the chip and the panel both say off.
	await expect(await chip(page)).toHaveText('sync: off');
	await expect(page.getByTestId('cloudsync-status-running')).toHaveText('no');
	await expect(page.getByTestId('cloudsync-status-configured')).toHaveText('no');
	await expect(page.getByTestId('cloudsync-sync-now')).toBeDisabled();
	await expect(page.getByTestId('cloudsync-journal-row')).toHaveCount(0);

	// 2. Save a config (not yet enabled, so the scheduler cannot race Sync now).
	await page.getByTestId('cloudsync-config-hub-url').fill(CLOUDSYNC_UI_HUB_URL);
	await page.getByTestId('cloudsync-config-machine-name').fill(SPOKE_NAME);
	const saved = page.waitForResponse(
		(r) => r.url().endsWith('/api/v1/cloudsync/config') && r.request().method() === 'PUT'
	);
	await page.getByTestId('cloudsync-config-save').click();
	expect((await saved).status()).toBe(200);
	const stored = await (await request.get(`${CLOUDSYNC_UI_ORIGIN}/api/v1/cloudsync/config`)).json();
	expect(stored.file).toEqual({ enabled: false, hub_url: CLOUDSYNC_UI_HUB_URL, machine_name: SPOKE_NAME });

	// 3. Sync now: one real round trip to the hub, then a journal row.
	const syncNow = page.getByTestId('cloudsync-sync-now');
	await expect(syncNow).toBeEnabled();
	const synced = page.waitForResponse(
		(r) => r.url().endsWith('/api/v1/cloudsync/sync') && r.request().method() === 'POST'
	);
	await syncNow.click();
	const syncResponse = await synced;
	expect(syncResponse.status()).toBe(200);
	const result = await syncResponse.json();
	expect(result.pushed).toBeGreaterThan(0);
	await expect(page.getByTestId('cloudsync-notice')).toContainText(`pushed ${result.pushed}`);
	const rows = page.getByTestId('cloudsync-journal-row');
	await expect(rows).toHaveCount(1);
	await expect(rows.first()).toHaveAttribute('data-status', /^(ok|inconclusive)$/);

	// The hub, asked directly, now knows this spoke by the name the form set.
	const hubMachines = await (await request.get(`${CLOUDSYNC_UI_HUB_URL}/api/v1/cloudsync/machines`)).json();
	expect(hubMachines.map((m: { name: string }) => m.name)).toContain(SPOKE_NAME);

	// 4. Fleet tab lists this machine, with titled counts.
	await page.getByRole('tab', { name: 'Fleet' }).click();
	await expect(page.locator(`[data-testid="cloudsync-fleet-row"][data-machine-name="${SPOKE_NAME}"]`)).toHaveCount(1);
	await expect(page.getByTestId('cloudsync-fleet').locator('.counts span').first()).toHaveAttribute('title', /enrolled/);

	// 5. Enable: the running scheduler beats within one beat interval (15s).
	await page.getByRole('tab', { name: 'Status & config' }).click();
	await page.getByTestId('cloudsync-config-enabled').check();
	await page.getByTestId('cloudsync-config-save').click();
	await expect(page.getByTestId('cloudsync-status-configured')).toHaveText('yes');
	await expect(async () => {
		await page.getByRole('button', { name: 'Refresh' }).click();
		await expect(page.getByTestId('cloudsync-status-running')).toHaveText('yes', { timeout: 2_000 });
	}).toPass({ timeout: 45_000, intervals: [3_000] });

	// With a fresh heartbeat the chip leaves 'off' (the positive control for
	// step 1) WITHOUT a reload: the save's status-changed event fired before the
	// first beat, and Refresh does not signal the chip, so only the chip's own
	// poll (CHIP_POLL_MS, 30s) can bring this in.
	await expect(await chip(page)).not.toHaveText('sync: off', { timeout: 40_000 });
	await page.reload();
	await expect(await chip(page)).not.toHaveText('sync: off');

	// The reload re-runs machines self-registration AFTER the spoke took its
	// configured name; it must keep that name, not collide back to hostname.
	await page.getByRole('tab', { name: 'Machines & policies' }).click();
	await expect(page.getByRole('cell', { name: SPOKE_NAME })).toBeVisible();

	// 6. Matrix: this spoke has no stored policy rows, so every cell reads an
	// explicit 'unset' (never 'stream') with its budget input disabled.
	const spokeRow = page.locator('table.matrix tbody tr', {
		has: page.getByRole('cell', { name: SPOKE_NAME, exact: true })
	});
	const cells = spokeRow.getByTestId('cloudsync-policy-cell');
	await expect(cells).toHaveCount(6);
	for (const cell of await cells.all()) {
		await expect(cell.locator('select')).toHaveValue('unset');
		await expect(cell.locator('input.budget')).toBeDisabled();
	}
	// Positive control: storing 'cached' on one cell is what enables its budget.
	const anlzCell = spokeRow.locator('[data-testid="cloudsync-policy-cell"][data-asset-kind="anlz_cache"]');
	const policySaved = page.waitForResponse(
		(r) => r.url().endsWith('/api/v1/cloudsync/policies') && r.request().method() === 'PUT'
	);
	await anlzCell.locator('select').selectOption('cached');
	expect((await policySaved).status()).toBe(200);
	await expect(anlzCell.locator('select')).toHaveValue('cached');
	await expect(anlzCell.locator('input.budget')).toBeEnabled();
	await expect(
		spokeRow.locator('[data-testid="cloudsync-policy-cell"][data-asset-kind="audio"] select')
	).toHaveValue('unset');
	expect(serverErrors).toEqual([]);
});

// requirement: CSSTATUS-04
// [if] CloudSync returns a real declared 409 [then] the status UI names the conflict and next step while raw diagnostics appear only under Technical details, [else stop]
test('a real declared 409 shows a plain conflict summary and technical details only on expand', async ({
	page,
	request
}) => {
	const spoke409 = `${SPOKE_NAME}-409`;
	const serverErrors: string[] = [];
	const syncStatuses: number[] = [];
	page.on('response', (r) => {
		if (r.status() >= 500) serverErrors.push(`${r.status()} ${r.request().method()} ${r.url()}`);
		if (r.url().endsWith('/api/v1/cloudsync/sync') && r.request().method() === 'POST') {
			syncStatuses.push(r.status());
		}
	});

	await page.goto(`${CLOUDSYNC_UI_ORIGIN}/cloudsync`);

	const configured = await request.put(`${CLOUDSYNC_UI_ORIGIN}/api/v1/cloudsync/config`, {
		data: { enabled: false, hub_url: CLOUDSYNC_UI_HUB_URL, machine_name: spoke409 }
	});
	expect(configured.status()).toBe(200);
	const configBody = await configured.json();
	expect(configBody.effective.hub_url).toBe(CLOUDSYNC_UI_HUB_URL);
	await page.reload();
	const syncNow = page.getByTestId('cloudsync-sync-now');
	await expect(syncNow).toBeEnabled();

	await page.evaluate(
		([hubUrl, machineName]) => {
			void fetch('/api/v1/cloudsync/sync', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ hub_url: hubUrl, name: machineName })
			});
		},
		[CLOUDSYNC_UI_HUB_URL, spoke409] as const
	);

	const refused = page.waitForResponse(
		(r) =>
			r.url().endsWith('/api/v1/cloudsync/sync') &&
			r.request().method() === 'POST' &&
			r.status() === 409
	);
	await syncNow.click();
	const refusedResponse = await refused;
	expect(refusedResponse.status()).toBe(409);
	await expect.poll(() => syncStatuses.filter((status) => status === 409).length).toBeGreaterThan(0);

	const notice = page.getByTestId('cloudsync-notice');
	await expect(notice).toContainText('Sync failed:');
	await expect(notice).toContainText(/another sync is already running/);
	await expect(notice).not.toContainText('HTTP 409');
	await expect(notice).not.toContainText(CLOUDSYNC_UI_HUB_URL);
	await page.getByTestId('cloudsync-notice-details').locator('summary').click();
	await expect(page.getByTestId('cloudsync-notice-details')).toContainText(/already running against this data dir/);

	expect(serverErrors).toEqual([]);
});
