/**
 * Headed e2e for in-app stems/lyrics job ordering (#1975).
 *
 * Acceptance:
 * - [if] two tracks are multi-selected and Stems: do next is chosen [then]
 *   GET /api/v1/library-jobs?lane=stems returns those ids in selection order
 *   and the ribbon or panel shows queued/running without reload.
 * - [if] a planted stem bundle exists [then] the job reports skipped: up to date.
 * - [if] a queued row is cancelled in the panel [then] it leaves the panel.
 * - [if] Lyrics: do next runs while a stems job is running [then] both lanes
 *   are active independently.
 */
import { expect, type Page, test } from '@playwright/test';
import { mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

function requireEnv(name: string, value: string | undefined): string {
	if (value === undefined || value === '') {
		throw new Error(`${name} must be set by playwright.library-jobs.config.ts`);
	}
	return value;
}

const API = requireEnv('LIBRARY_JOBS_E2E_API_BASE', process.env.LIBRARY_JOBS_E2E_API_BASE);
const DATA_DIR = requireEnv('LIBRARY_JOBS_E2E_DATA_DIR', process.env.LIBRARY_JOBS_E2E_DATA_DIR);

interface LibraryJobItem {
	stable_id: string;
	state: string;
	detail: string | null;
	reason: string | null;
}

interface LibraryJobList {
	items: LibraryJobItem[];
}

test.describe.configure({ mode: 'serial' });

const modifier = process.platform === 'darwin' ? 'Meta' : 'Control';

async function trackIds(page: Page): Promise<[string, string]> {
	const rows = page.locator('[data-testid="track-row"]');
	const id0 = await rows.nth(0).getAttribute('data-stable-id');
	const id1 = await rows.nth(1).getAttribute('data-stable-id');
	if (id0 === null || id1 === null) {
		throw new Error('expected two track rows with data-stable-id');
	}
	return [id0, id1];
}

async function listLane(
	page: Page,
	lane: 'stems' | 'lyrics',
	includeSettled = false
): Promise<LibraryJobList> {
	const query = includeSettled ? '?lane=' + lane + '&include=settled' : '?lane=' + lane;
	const response = await page.request.get(`${API}/api/v1/library-jobs${query}`);
	expect(response.ok()).toBeTruthy();
	return (await response.json()) as LibraryJobList;
}

async function selectTwo(page: Page): Promise<void> {
	const rows = page.locator('[data-testid="track-row"]');
	await rows.nth(0).click();
	await rows.nth(1).click({ modifiers: [modifier] });
	await expect(rows.nth(0)).toHaveClass(/rb-row-selected/);
	await expect(rows.nth(1)).toHaveClass(/rb-row-selected/);
}

async function doNext(page: Page, label: string): Promise<void> {
	const row = page.locator('[data-testid="track-row"].rb-row-selected').first();
	await row.click({ button: 'right' });
	await page.getByRole('menuitem', { name: label }).click();
}

async function openPanel(page: Page): Promise<void> {
	// The job ribbon itself (LibraryJobsChrome.svelte), by its own class. A
	// role-and-name match on /stems |lyrics / also takes the library health
	// dots and the stem-cache dot, whose labels name the same lanes.
	const ribbon = page.locator('button.job-ribbon');
	await expect(ribbon).toBeVisible({ timeout: 15_000 });
	await ribbon.click();
	await expect(page.getByTestId('library-job-queue-panel')).toBeVisible();
}

async function cancelQueued(page: Page, stableId: string): Promise<void> {
	const row = page
		.getByTestId('library-job-queue-panel')
		.locator('[data-testid="library-job-row"][data-stable-id="' + stableId + '"]');
	await row.getByRole('button', { name: 'Cancel' }).click();
}

async function cancelActiveLane(page: Page, lane: 'stems' | 'lyrics'): Promise<void> {
	const listing = await listLane(page, lane, true);
	for (const item of listing.items) {
		if (item.state !== 'pending' && item.state !== 'running') continue;
		const response = await page.request.post(
			`${API}/api/v1/library-jobs/${lane}/${item.stable_id}/cancel`
		);
		if (response.ok()) continue;
		// The dry runner can settle a job between the listing above and this
		// POST, and the daemon refuses to cancel one that is no longer active.
		// A job that finished on its own is off the lane, which is the whole
		// point of this teardown. A job still pending or running after a
		// refused cancel is not, and that is a real failure.
		const now = (await listLane(page, lane, true)).items.find(
			(row) => row.stable_id === item.stable_id
		);
		expect(
			now !== undefined && now.state !== 'pending' && now.state !== 'running',
			`cancel ${lane}/${item.stable_id} returned ${response.status()} while it was still active`
		).toBeTruthy();
	}
}

function plantStemBundle(stableId: string): void {
	const bundleDir = join(DATA_DIR, 'state', 'stems', stableId);
	mkdirSync(bundleDir, { recursive: true });
	writeFileSync(join(bundleDir, 'manifest.json'), '{}', 'utf8');
}

function removeStemBundle(stableId: string): void {
	rmSync(join(DATA_DIR, 'state', 'stems', stableId), { recursive: true, force: true });
}

test.beforeEach(async ({ page }) => {
	// This suite runs a REAL engine, which lists the host's own USB volumes.
	// On a machine with a stick or an external disk mounted the "USB sticks"
	// panel opens by itself over the job queue panel and takes its clicks.
	// The suite is about library jobs, so the host's volumes are answered as
	// none (same body as the engine's empty scan).
	await page.route('**/api/v1/usb/volumes', (route) =>
		route.fulfill({ json: { volumes: [], scanned_at: 0 } })
	);
	await page.goto('/performance');
	await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible({
		timeout: 30_000
	});
});

test('stems do-next preserves selection order and live status', async ({ page }) => {
	const [id0, id1] = await trackIds(page);
	await selectTwo(page);
	await doNext(page, 'Stems: do next');

	await expect
		.poll(async () => {
			const listing = await listLane(page, 'stems');
			return listing.items.map((item) => item.stable_id);
		})
		.toEqual([id0, id1]);

	const listing = await listLane(page, 'stems');
	for (const item of listing.items) {
		expect(['pending', 'running']).toContain(item.state);
	}

	await expect
		.poll(async () => {
			const active = await listLane(page, 'stems');
			return active.items.some((item) => item.state === 'running');
		})
		.toBeTruthy();

	expect(page.url()).toContain('/performance');

	await openPanel(page);
	const panel = page.getByTestId('library-job-queue-panel');
	await expect(panel.locator('[data-stable-id="' + id0 + '"]')).toBeVisible();
	await expect(panel.locator('[data-stable-id="' + id1 + '"]')).toBeVisible();

	await cancelActiveLane(page, 'stems');
});

test('planted bundle is skipped up to date', async ({ page }) => {
	const [id0, id1] = await trackIds(page);
	plantStemBundle(id0);
	await selectTwo(page);
	await doNext(page, 'Stems: do next');

	await expect
		.poll(async () => {
			const listing = await listLane(page, 'stems', true);
			const skipped = listing.items.find((item) => item.stable_id === id0);
			return skipped?.state === 'skipped';
		})
		.toBeTruthy();

	const settled = await listLane(page, 'stems', true);
	const skipped = settled.items.find((item) => item.stable_id === id0);
	expect(skipped).toBeDefined();
	expect(skipped?.detail ?? '').toContain('skipped: up to date');

	const active = settled.items.find((item) => item.stable_id === id1);
	expect(active).toBeDefined();
	expect(['pending', 'running']).toContain(active?.state);

	const liveOnly = await listLane(page, 'stems');
	expect(liveOnly.items.some((item) => item.stable_id === id0)).toBeFalsy();

	await cancelActiveLane(page, 'stems');
	removeStemBundle(id0);
});

test('cancel queued row leaves the panel', async ({ page }) => {
	const [id0, id1] = await trackIds(page);
	await selectTwo(page);
	await doNext(page, 'Stems: do next');

	let queuedId = '';
	await expect
		.poll(async () => {
			const listing = await listLane(page, 'stems');
			const pending = listing.items.find((item) => item.state === 'pending');
			const running = listing.items.some((item) => item.state === 'running');
			if (pending !== undefined && running) {
				queuedId = pending.stable_id;
				return true;
			}
			return false;
		})
		.toBeTruthy();

	await openPanel(page);
	await cancelQueued(page, queuedId);
	await expect(
		page
			.getByTestId('library-job-queue-panel')
			.locator('[data-stable-id="' + queuedId + '"]')
	).toHaveCount(0);

	const active = await listLane(page, 'stems');
	expect(active.items.some((item) => item.stable_id === queuedId)).toBeFalsy();

	// Read the SETTLED listing for what the cancel did. Whether the sibling is
	// still ACTIVE by the time the panel round-trip finishes is a stopwatch
	// question about the dry hold, not an acceptance one; what the cancel must
	// have done is exact, and stays exact however long the interaction took.
	const after = await listLane(page, 'stems', true);
	const cancelledRow = after.items.find((item) => item.stable_id === queuedId);
	expect(cancelledRow?.state).toBe('cancelled');
	const sibling = after.items.find((item) => item.stable_id !== queuedId);
	expect(sibling, 'the other selected job must still be on the lane').toBeDefined();
	expect(sibling?.state).not.toBe('cancelled');

	await cancelActiveLane(page, 'stems');
});

test('lyrics do-next while stems is running uses a separate lane', async ({ page }) => {
	const [id0, id1] = await trackIds(page);
	await selectTwo(page);
	await doNext(page, 'Stems: do next');

	await expect
		.poll(async () => {
			const listing = await listLane(page, 'stems');
			return listing.items.some((item) => item.state === 'running');
		})
		.toBeTruthy();

	const lyricsTarget = id1;
	await doNext(page, 'Lyrics: do next');

	await expect
		.poll(async () => {
			const listing = await listLane(page, 'lyrics');
			const item = listing.items.find((row) => row.stable_id === lyricsTarget);
			return item !== undefined && ['pending', 'running'].includes(item.state);
		})
		.toBeTruthy();

	const stemsDuring = await listLane(page, 'stems');
	expect(stemsDuring.items.some((item) => item.state === 'running')).toBeTruthy();

	await openPanel(page);
	const panel = page.getByTestId('library-job-queue-panel');
	await panel.getByRole('button', { name: 'Lyrics' }).click();
	await expect(panel.locator('[data-stable-id="' + lyricsTarget + '"]')).toBeVisible();
	await panel.getByRole('button', { name: 'Stems' }).click();
	await expect(panel.locator('[data-stable-id="' + id0 + '"], [data-stable-id="' + id1 + '"]')).not.toHaveCount(0);

	await cancelActiveLane(page, 'lyrics');
	await cancelActiveLane(page, 'stems');
});
