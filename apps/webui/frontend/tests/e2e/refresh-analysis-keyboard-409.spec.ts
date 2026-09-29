/**
 * Refresh analysis activated from the keyboard while a refresh already runs
 * (CHROME-10, Codex P2 4130152643 on PR #3896).
 *
 * The failed click opened the popover (hovered = true) but only mouseenter
 * started the status polling, so a keyboard user got "Already running" and no
 * live progress. Nothing but the pointer could close it either.
 *
 * Root suite, real engine: the 409 is the engine's own answer to a second
 * POST /api/v1/ingest/refresh while the job this test starts is still running
 * (a library sweep over the fixture's two tracks, a few hundred ms here). The
 * page's own POST is watched, so an attempt that lost that race (202) is
 * retried on a fresh page, never read as a 409.
 *
 * Counted on the wire: GET /ingest/config is fetched only when the popover
 * opens (onEnter), GET /ingest/refresh/status only by its polling.
 *
 * [if] a keyboard 409 opens the popover without polling [then] stop.
 * [if] the popover closes (Escape, blur, or the mouse leaving) and polling goes
 *   on once the job is over [then] stop.
 * control [if] a hovered mouse click's 409 opens it a second time or runs a
 *   second timer [then] stop.
 */
// requirement: CHROME-10
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

const STATUS = '/api/v1/ingest/refresh/status';
const CONFIG = '/api/v1/ingest/config';

type Wire = { status: number[]; config: number[]; posts: number[] };

/** Timestamps of the page's own ingest GETs, and its refresh POST statuses. */
function watchWire(page: Page): Wire {
	const wire: Wire = { status: [], config: [], posts: [] };
	page.on('request', (r) => {
		if (r.method() !== 'GET') return;
		const path = new URL(r.url()).pathname;
		if (path === STATUS) wire.status.push(Date.now());
		else if (path === CONFIG) wire.config.push(Date.now());
	});
	page.on('response', (r) => {
		const req = r.request();
		if (req.method() === 'POST' && new URL(r.url()).pathname === '/api/v1/ingest/refresh') {
			wire.posts.push(r.status());
		}
	});
	return wire;
}

async function waitIdle(request: APIRequestContext): Promise<void> {
	await expect
		.poll(async () => ((await (await request.get(STATUS)).json()) as { running: boolean }).running, {
			timeout: 30_000
		})
		.toBe(false);
}

/** Start a real refresh on the engine and report it running. */
async function startRunning(request: APIRequestContext): Promise<void> {
	await waitIdle(request);
	const res = await request.post('/api/v1/ingest/refresh', { data: {} });
	expect(res.status(), await res.text()).toBe(202);
}

/** Closed, the popover's polling stops. The status poll also runs while the
 * page last saw a job running (by design), so let the engine's job end and
 * the page's next poll see that first; after that nothing may poll. */
async function expectPollingStops(page: Page, request: APIRequestContext, wire: Wire): Promise<void> {
	await waitIdle(request);
	await page.waitForTimeout(1_500);
	const closedAt = wire.status.length;
	await page.waitForTimeout(2_600);
	expect(wire.status.length, 'closed and idle: no timer left running').toBe(closedAt);
}

/** Open /performance with a real running refresh, then activate the button
 * the way `activate` says, until the page's own POST is answered 409. */
async function clickInto409(
	page: Page,
	request: APIRequestContext,
	activate: (btn: ReturnType<Page['getByTestId']>) => Promise<void>,
	prepare: (btn: ReturnType<Page['getByTestId']>) => Promise<void>
): Promise<Wire> {
	for (let attempt = 1; attempt <= 4; attempt++) {
		await page.goto('/performance');
		const wire = watchWire(page);
		const btn = page.getByTestId('refresh-analysis');
		await expect(btn).toBeVisible({ timeout: 30_000 });
		await prepare(btn);
		await startRunning(request);
		await activate(btn);
		await expect.poll(() => wire.posts.length, { timeout: 10_000 }).toBe(1);
		if (wire.posts[0] === 409) return wire;
		// The engine finished before the page's POST landed and started a new
		// run instead: not the case under test, so let it end and go again.
		console.log(`[refresh-409] attempt ${attempt} lost the race: page POST got ${wire.posts[0]}`);
		await waitIdle(request);
	}
	throw new Error('no attempt reached the engine while its refresh was running');
}

test.describe('refresh analysis 409 from the keyboard', () => {
	test.setTimeout(180_000);
	let savedEnabled: Record<string, boolean> | null = null;

	test.beforeEach(async ({ request }) => {
		const cfg = (await (await request.get(CONFIG)).json()) as { steps: { id: string; enabled: boolean }[] };
		savedEnabled = Object.fromEntries(cfg.steps.map((s) => [s.id, s.enabled]));
		// The runnable step on this fixture (ingest-refresh.spec.ts does the same).
		const put = await request.put(CONFIG, { data: { enabled: { analysis: true, stems: false, vocals: false } } });
		expect(put.ok()).toBe(true);
	});

	test.afterEach(async ({ request }) => {
		await waitIdle(request);
		if (savedEnabled !== null) await request.put(CONFIG, { data: { enabled: savedEnabled } });
	});

	test('the popover a keyboard 409 opens polls live progress, and Escape stops it', async ({ page, request }) => {
		const wire = await clickInto409(
			page,
			request,
			(btn) => btn.press('Enter'),
			(btn) => btn.focus()
		);
		const btn = page.getByTestId('refresh-analysis');
		expect(await btn.evaluate((el) => el.parentElement?.matches(':hover')), 'no pointer involved').toBe(false);

		const pop = page.getByTestId('refresh-analysis-pop');
		await expect(pop).toBeVisible();
		await expect(page.getByTestId('refresh-click-feedback')).toHaveText('Already running');
		// The polled status is what renders the phase line and progress bar.
		await expect(pop.locator('.bar')).toBeVisible({ timeout: 5_000 });
		expect(wire.config.length, 'opened once, as a hover opens it').toBe(1);

		// Still open, so still polling at 1 Hz.
		const before = wire.status.length;
		await page.waitForTimeout(2_600);
		expect(wire.status.length - before).toBeGreaterThanOrEqual(2);

		await btn.press('Escape');
		await expect(pop).toBeHidden();
		await expectPollingStops(page, request, wire);
	});

	test('blur closes a keyboard-opened popover and stops its polling', async ({ page, request }) => {
		const wire = await clickInto409(
			page,
			request,
			(btn) => btn.press('Enter'),
			(btn) => btn.focus()
		);
		const pop = page.getByTestId('refresh-analysis-pop');
		await expect(pop.locator('.bar')).toBeVisible({ timeout: 5_000 });

		await page.getByTestId('refresh-analysis').evaluate((el) => (el as HTMLElement).blur());
		await expect(pop).toBeHidden();
		await expectPollingStops(page, request, wire);
	});

	test('control: a hovered mouse click that 409s opens nothing twice and runs one timer', async ({ page, request }) => {
		const wire = await clickInto409(
			page,
			request,
			(btn) => btn.click(),
			async (btn) => {
				await btn.hover();
				await expect(page.getByTestId('refresh-analysis-pop')).toBeVisible();
			}
		);
		const pop = page.getByTestId('refresh-analysis-pop');
		await expect(page.getByTestId('refresh-click-feedback')).toHaveText('Already running');
		await expect(pop.locator('.bar')).toBeVisible({ timeout: 5_000 });
		expect(wire.config.length, 'the hover opened it; the 409 did not open it again').toBe(1);

		// One 1 Hz timer: about 4 polls in 4 s. Two would put about 8 on the wire.
		const before = wire.status.length;
		await page.waitForTimeout(4_000);
		const polls = wire.status.length - before;
		expect(polls).toBeGreaterThanOrEqual(3);
		expect(polls).toBeLessThanOrEqual(5);

		await page.mouse.move(0, 0);
		await expect(pop).toBeHidden();
		await expectPollingStops(page, request, wire);
	});
});
