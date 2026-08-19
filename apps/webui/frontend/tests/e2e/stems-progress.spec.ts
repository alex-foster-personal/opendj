/**
 * The stems loading bar, driven by a real job through the real pipeline.
 *
 * What this proves that a unit test cannot: that a job enqueued OUTSIDE the
 * page (over HTTP, exactly as the install-flow prompt or an agent would)
 * reaches the browser as live progress. That path crosses the runner's fork,
 * the worker's stdout protocol, the store, the WS hub, the events bus and the
 * jobs store before it ever reaches a pixel, and every one of those is real
 * here. Only the Modal call is substituted, by the config.
 *
 * Regression lines:
 *  - if the bar never appears then the WS path from worker to TopBar is broken
 *  - if the bar has no hover title then a live number has no explanation
 *  - if the bar never retires then a finished run looks like a stuck one
 *  - if the bundles do not land then the job reported progress it did not make
 */
import { expect, test } from '@playwright/test';

const API = process.env.STEMS_E2E_API_BASE ?? '';

test.describe.configure({ mode: 'serial' });

test('a stems job enqueued over HTTP drives the TopBar bar and then retires it', async ({
	page
}) => {
	expect(API, 'the config must publish STEMS_E2E_API_BASE').not.toBe('');

	await page.goto('/performance');

	// WAIT FOR THE APP TO BE WATCHING BEFORE GIVING IT SOMETHING TO WATCH.
	// Every jobs surface is gated on one memoised GET /api/v1/health probe,
	// and until it answers, jobsRefusal() is non-null and the store has not
	// subscribed to anything. Enqueuing before that point is a race the test
	// loses in the worst way: the job runs to completion unobserved and the
	// bar is correctly never shown. The JOBS toggle going live is the
	// app's own signal that the probe has landed.
	await expect(page.getByRole('button', { name: 'Jobs drawer' })).toBeEnabled();

	const bar = page.locator('.stems-progress');
	// Nothing running yet, so the bar must not be occupying the menu bar.
	await expect(bar).toHaveCount(0);

	// The plan is what the install prompt reads. Assert it sees real work
	// before enqueuing any, so a green run cannot come from an empty library.
	const planBefore = await (await page.request.get(`${API}/api/v1/stems/plan?tier=M`)).json();
	expect(planBefore.pending).toBeGreaterThan(0);
	expect(planBefore.total).toBe(planBefore.pending + planBefore.ready + planBefore.unavailable);

	// Enqueue exactly as the prompt does: a scope, not a frozen id list.
	const created = await page.request.post(`${API}/api/v1/jobs`, {
		data: { kind: 'stems.separate', payload: { scope: 'pending', tier: 'M' } }
	});
	expect(created.status()).toBe(201);
	const job = await created.json();

	// 1. The bar arrives without a reload, i.e. over the socket.
	await expect(bar).toBeVisible();

	// 2. It explains itself. House rule: a numeric readout carries a title.
	const title = await bar.getAttribute('title');
	expect(title).toMatch(/Stems separation/);
	expect(title).toMatch(/costs real money/i);

	const meter = bar.locator('[role="progressbar"]');
	await expect(meter).toHaveAttribute('aria-valuemax', '100');

	// 3. It retires when the work is done, rather than sitting at 100%.
	await expect(bar).toHaveCount(0, { timeout: 90_000 });

	// 4. The bar was telling the truth: the job really succeeded and the
	//    plan has moved, so progress tracked artifacts and not a timer.
	const finished = await (await page.request.get(`${API}/api/v1/jobs/${job.id}`)).json();
	expect(finished.status, finished.error ?? '').toBe('succeeded');

	const planAfter = await (await page.request.get(`${API}/api/v1/stems/plan?tier=M`)).json();
	expect(planAfter.pending).toBe(0);
	expect(planAfter.ready).toBe(planBefore.pending + planBefore.ready);
});
