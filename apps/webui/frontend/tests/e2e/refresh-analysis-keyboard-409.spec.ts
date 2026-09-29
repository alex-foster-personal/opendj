/**
 * Refresh analysis activated from the keyboard (CHROME-10, Codex P2
 * 4130152643, 4130407830 and 4130581613 on PR #3896).
 *
 * A failed click opened the popover (hovered = true) but only mouseenter
 * started the status polling, so a keyboard user got "Already running" and no
 * live progress; a click that started a run opened nothing at all. Nothing
 * but the pointer could close it either.
 *
 * Root suite, real engine: the 409 is the engine's own answer to the page's
 * POST /api/v1/ingest/refresh while another job holds the one refresh slot.
 * That job is a real one-track analysis order (POST /analysis-queue/orders,
 * the analysis grid's own API), so it always runs the real analysis CLI; a
 * library sweep did not, because once an earlier spec has analyzed the
 * fixture it has no targets and ends in milliseconds (CI run 36534163620:
 * every attempt lost). The page's POST is fired first and held with
 * page.route; the order is started; the hold is released only once the
 * status endpoint reports the job running with its CLI launched, and the
 * held POST then goes on to the engine unchanged. An attempt whose POST still
 * got 202 is logged and retried, never read as a 409, and three misses fail
 * loudly.
 *
 * Counted on the wire: GET /ingest/config is fetched only when the popover
 * opens (onEnter), GET /ingest/refresh/status only by its polling.
 *
 * [if] a keyboard click that starts a run (202) opens no polled popover [then] stop.
 * [if] a keyboard 409 opens the popover without polling [then] stop.
 * [if] a popover dismissed (Escape, or the mouse leaving) while the click's
 *   POST is pending comes back when the POST settles [then] stop. The POST is
 *   held with page.route and then continued to the real engine, unchanged.
 *   The started run still polls while it lasts (its done toast proves it).
 * [if] a status GET the engine answered before the run started, delivered to
 *   the page after the POST settled, puts the idle status back and the run
 *   stops being followed [then] stop (Codex P2 4130868861). The GET's real
 *   answer is only delayed by page.route (route.fetch, then fulfill with that
 *   same response), never rewritten.
 * [if] a running status of a run, answered before that run's end reached the
 *   page and delivered after it (two polls in flight at once), is applied: the
 *   run is followed again and its end toasts a second time [then] stop (Codex
 *   P2 4131292228). The held answer is the engine's own, delayed, not rewritten.
 * control [if] a newer running status of the run already shown is dropped
 *   (progress frozen until the run ends) [then] stop: the ordering overshot.
 * [if] the popover closes (Escape, blur, or the mouse leaving) and polling goes
 *   on once the job is over [then] stop.
 * control [if] a hovered mouse click's 409 opens it a second time or runs a
 *   second timer [then] stop.
 */
// requirement: CHROME-10
import { expect, test, type APIRequestContext, type Page, type Route } from '@playwright/test';

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

/** Order one real analysis CLI run on a fixture track, through the same
 * single slot a refresh claims. True once the engine reports that job running
 * with its CLI launched; false if it ended before that was seen. */
async function startCliRun(request: APIRequestContext): Promise<boolean> {
	await waitIdle(request);
	const tracks = (await (await request.get('/api/v1/tracks?limit=1')).json()) as {
		items: { stable_id: string }[];
	};
	const sid = tracks.items[0]?.stable_id;
	expect(sid, 'the fixture library has a track to analyze').toBeTruthy();
	const res = await request.post(`/api/v1/analysis-queue/orders/${encodeURIComponent(sid)}/key`);
	expect(res.status(), await res.text()).toBe(202);
	const deadline = Date.now() + 30_000;
	while (Date.now() < deadline) {
		const s = (await (await request.get(STATUS)).json()) as { running: boolean; log_tail: string[] };
		if (!s.running) return false;
		if (s.log_tail.some((l) => l.includes('apps.analysis.run'))) return true;
	}
	throw new Error('the ordered analysis job neither launched its CLI nor ended in 30 s');
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

/** Make the page's own library sweep launch a real CLI (the vocals step runs
 * its from-stems CLI whatever the target count), so the POST reliably answers
 * with the run still going. An analysis-only sweep has no targets once an
 * earlier spec analyzed the fixture and can be over before the POST answers. */
async function sweepRunsCli(request: APIRequestContext): Promise<void> {
	const put = await request.put(CONFIG, { data: { enabled: { analysis: false, stems: false, vocals: true } } });
	expect(put.ok()).toBe(true);
}

/** Hold the page's refresh POST until release(), then send it on to the real
 * engine unchanged. Nothing is fulfilled or rewritten here. */
async function holdRefreshPost(
	page: Page
): Promise<{ arrived: Promise<void>; release: () => void; stop: () => Promise<void> }> {
	let release = (): void => {};
	const gate = new Promise<void>((r) => (release = r));
	let arrive = (): void => {};
	const arrived = new Promise<void>((r) => (arrive = r));
	const matcher = (url: URL): boolean => url.pathname === '/api/v1/ingest/refresh';
	await page.route(matcher, async (route) => {
		if (route.request().method() === 'POST') {
			arrive();
			await gate;
		}
		await route.continue();
	});
	return { arrived, release: () => release(), stop: () => page.unroute(matcher) };
}

/** A dismissal made while the POST was held stays made once it settles. */
async function expectStaysDismissed(page: Page, request: APIRequestContext, wire: Wire, release: () => void) {
	const pop = page.getByTestId('refresh-analysis-pop');
	const answered = page.waitForResponse(
		(r) => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/v1/ingest/refresh'
	);
	release();
	const started = (await (await answered).json()) as { running: boolean };
	await expect.poll(() => wire.posts.length, { timeout: 10_000 }).toBe(1);
	expect(wire.posts[0], 'the held POST reached the real engine and started a run').toBe(202);
	expect(started.running, 'the sweep runs a CLI, so it is still going when the POST answers').toBe(true);
	await page.waitForTimeout(1_500);
	await expect(pop).toBeHidden();
	expect(wire.config.length, 'nothing opened the popover again').toBe(1);
	// The run is still followed while it lasts: its done toast comes only
	// from the status polling.
	await expect(page.getByText(/Refresh (done|failed):/).first()).toBeVisible({ timeout: 30_000 });
	await expect(pop).toBeHidden();
	await expectPollingStops(page, request, wire);
}

/** Activate the button the way `activate` says, holding the page's POST until
 * a real analysis job provably occupies the slot, so the engine answers 409. */
async function clickInto409(
	page: Page,
	request: APIRequestContext,
	activate: (btn: ReturnType<Page['getByTestId']>) => Promise<void>,
	prepare: (btn: ReturnType<Page['getByTestId']>) => Promise<void>
): Promise<Wire> {
	for (let attempt = 1; attempt <= 3; attempt++) {
		await page.goto('/performance');
		const wire = watchWire(page);
		const btn = page.getByTestId('refresh-analysis');
		await expect(btn).toBeVisible({ timeout: 30_000 });
		await prepare(btn);
		await waitIdle(request);
		const held = await holdRefreshPost(page);
		await activate(btn);
		await held.arrived;
		const running = await startCliRun(request);
		held.release();
		await expect.poll(() => wire.posts.length, { timeout: 10_000 }).toBe(1);
		await held.stop();
		if (running && wire.posts[0] === 409) return wire;
		// The job ended before it was seen running, or before the released POST
		// reached the engine: not the case under test, so log it and go again.
		console.log(`[refresh-409] attempt ${attempt} missed: job seen running ${running}, page POST got ${wire.posts[0]}`);
		await waitIdle(request);
	}
	throw new Error('no attempt reached the engine while its refresh was running');
}

test.describe('refresh analysis clicked from the keyboard', () => {
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

	test('a keyboard click that starts a run opens the popover with polled progress', async ({ page, request }) => {
		await page.goto('/performance');
		const wire = watchWire(page);
		const btn = page.getByTestId('refresh-analysis');
		await expect(btn).toBeVisible({ timeout: 30_000 });
		await btn.focus();
		await waitIdle(request);
		await btn.press('Enter');
		await expect.poll(() => wire.posts.length, { timeout: 10_000 }).toBe(1);
		expect(wire.posts[0], 'the engine started this run for the page').toBe(202);
		expect(await btn.evaluate((el) => el.parentElement?.matches(':hover')), 'no pointer involved').toBe(false);

		const pop = page.getByTestId('refresh-analysis-pop');
		await expect(pop).toBeVisible();
		await expect(page.getByTestId('refresh-click-feedback')).toHaveCount(0);
		// Live progress: the open popover keeps polling after the POST settled,
		// and the run's terminal phase is what it shows.
		const settledAt = wire.status.length;
		await expect.poll(() => wire.status.length, { timeout: 5_000 }).toBeGreaterThan(settledAt);
		await expect(pop.locator('.pop-phase')).toHaveText(/^\s*(done|error)\b/, { timeout: 30_000 });
		expect(wire.config.length, 'opened once, as a hover opens it').toBe(1);

		await btn.press('Escape');
		await expect(pop).toBeHidden();
		await expectPollingStops(page, request, wire);
	});

	test('Escape while the POST is pending keeps the popover dismissed after it settles', async ({ page, request }) => {
		await sweepRunsCli(request);
		await page.goto('/performance');
		const wire = watchWire(page);
		const held = await holdRefreshPost(page);
		const btn = page.getByTestId('refresh-analysis');
		await expect(btn).toBeVisible({ timeout: 30_000 });
		await btn.focus();
		await waitIdle(request);
		await btn.press('Enter');
		await held.arrived;
		const pop = page.getByTestId('refresh-analysis-pop');
		await expect(pop, 'the click opened it before the POST settled').toBeVisible();
		await btn.press('Escape');
		await expect(pop).toBeHidden();
		await expectStaysDismissed(page, request, wire, held.release);
	});

	test('the mouse leaving while the POST is pending keeps the popover dismissed', async ({ page, request }) => {
		await sweepRunsCli(request);
		await page.goto('/performance');
		const wire = watchWire(page);
		const held = await holdRefreshPost(page);
		const btn = page.getByTestId('refresh-analysis');
		await expect(btn).toBeVisible({ timeout: 30_000 });
		await waitIdle(request);
		await btn.hover();
		const pop = page.getByTestId('refresh-analysis-pop');
		await expect(pop).toBeVisible();
		await btn.click();
		await held.arrived;
		await page.mouse.move(0, 0);
		await expect(pop).toBeHidden();
		await expectStaysDismissed(page, request, wire, held.release);
	});

	test('an earlier running status of a run, delivered after its end, neither revives it nor toasts twice', async ({
		page,
		request
	}) => {
		type Snap = { running: boolean; started_at: number | null; finished_at: number | null };
		type Parked = { route: Route; sent: number };
		const toasts = () => page.evaluate(() => (window as unknown as { __refreshToasts: number }).__refreshToasts);
		for (let attempt = 1; attempt <= 3; attempt++) {
			await page.goto('/performance');
			const wire = watchWire(page);
			const btn = page.getByTestId('refresh-analysis');
			await expect(btn).toBeVisible({ timeout: 30_000 });
			// Every toast the page inserts, counted as it appears: one that has
			// already auto-dismissed still counts.
			await page.evaluate(() => {
				const w = window as unknown as { __refreshToasts: number };
				w.__refreshToasts = 0;
				const seen = new WeakSet<Node>();
				new MutationObserver((records) => {
					for (const r of records)
						for (const n of r.addedNodes)
							if (!seen.has(n) && /Refresh (done|failed):/.test(n.textContent ?? '')) {
								seen.add(n);
								w.__refreshToasts += 1;
							}
				}).observe(document.body, { childList: true, subtree: true });
			});
			await waitIdle(request);

			// Park every status request the page sends (the request leg is slow),
			// until passThrough, after which each goes straight to the engine.
			const parked: Parked[] = [];
			let passThrough = false;
			const matcher = (url: URL): boolean => url.pathname === STATUS;
			await page.route(matcher, async (route) => {
				if (passThrough) return route.continue();
				parked.push({ route, sent: Date.now() });
			});
			await btn.hover(); // onEnter polls now, then every second while hovered
			await expect.poll(() => parked.length, { timeout: 5_000 }).toBeGreaterThanOrEqual(2);
			const ran = await startCliRun(request);
			// Two of the page's polls reach the engine together while the run is
			// going: both are its own answers for this run.
			const [first, second] = parked.splice(0, 2);
			const [r1, r2] = await Promise.all([first.route.fetch(), second.route.fetch()]);
			const [a, b] = [(await r1.json()) as Snap, (await r2.json()) as Snap];
			if (!ran || !a.running || !b.running || a.started_at !== b.started_at) {
				console.log(`[refresh-overlap] attempt ${attempt} missed: ran ${ran}, answers running ${a.running}/${b.running}`);
				await first.route.fulfill({ response: r1 });
				await second.route.fulfill({ response: r2 });
				passThrough = true;
				for (const p of parked.splice(0)) await p.route.continue();
				await page.unroute(matcher);
				await page.mouse.move(0, 0);
				await waitIdle(request);
				continue;
			}
			await first.route.fulfill({ response: r1 }); // the page follows the run
			passThrough = true;
			for (const p of parked.splice(0)) await p.route.continue();
			await waitIdle(request);
			// The run's end reaches the page by its polling: one toast.
			await expect.poll(toasts, { timeout: 30_000 }).toBe(1);
			const endedAt = ((await (await request.get(STATUS)).json()) as Snap).finished_at;
			expect(endedAt, 'the run the page followed has ended').not.toBeNull();

			// Now the other overlapping poll's answer arrives: an earlier running
			// status of that same run.
			const polls = wire.status.length;
			await second.route.fulfill({ response: r2 });
			await expect.poll(() => wire.status.length, { timeout: 5_000 }).toBeGreaterThan(polls + 1);
			await page.waitForTimeout(1_500);
			expect(await toasts(), 'the stale running status revived the run and its end toasted again').toBe(1);
			await expect(btn).not.toHaveClass(/running/);

			await page.unroute(matcher);
			await page.mouse.move(0, 0);
			await expectPollingStops(page, request, wire);
			return;
		}
		throw new Error('no attempt got two running answers of one run from the engine');
	});

	test('control: a newer running status of the same run still applies, so progress keeps updating', async ({
		page,
		request
	}) => {
		// The overshoot of the ordering above is dropping a snapshot of the run
		// already shown; mid-run progress (log, step count, badges) would then
		// freeze until the run ends.
		type Snap = { running: boolean; started_at: number | null; log_tail: string[] };
		await sweepRunsCli(request);
		for (let attempt = 1; attempt <= 3; attempt++) {
			await page.goto('/performance');
			const wire = watchWire(page);
			const btn = page.getByTestId('refresh-analysis');
			await expect(btn).toBeVisible({ timeout: 30_000 });
			const parked: Route[] = [];
			let passThrough = false;
			const matcher = (url: URL): boolean => url.pathname === STATUS;
			await page.route(matcher, async (route) => {
				if (passThrough) return route.continue();
				parked.push(route);
			});
			const release = async (): Promise<void> => {
				passThrough = true;
				for (const r of parked.splice(0)) await r.continue();
			};
			await btn.focus();
			await waitIdle(request);
			const answered = page.waitForResponse(
				(r) => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/v1/ingest/refresh'
			);
			await btn.press('Enter'); // opens (its poll is parked) and POSTs
			const started = (await (await answered).json()) as Snap;
			await expect.poll(() => parked.length, { timeout: 5_000 }).toBeGreaterThanOrEqual(1);
			// Wait for the engine's log for this run to grow past what the POST
			// carried, then let one parked poll reach it.
			let grown = false;
			for (let i = 0; i < 200 && !grown; i++) {
				const now = (await (await request.get(STATUS)).json()) as Snap;
				if (!now.running) break;
				grown = now.log_tail.length > started.log_tail.length;
			}
			const route = parked.shift()!;
			const res = await route.fetch();
			const newer = (await res.json()) as Snap;
			const line = newer.log_tail.at(-1) ?? '';
			if (
				!started.running ||
				!grown ||
				!newer.running ||
				newer.started_at !== started.started_at ||
				started.log_tail.includes(line)
			) {
				console.log(`[refresh-progress] attempt ${attempt} missed: log grew ${grown}, newer running ${newer.running}`);
				await route.fulfill({ response: res });
				await release();
				await page.unroute(matcher);
				await waitIdle(request);
				continue;
			}
			await route.fulfill({ response: res });
			// Every later poll is still parked, so only that answer can show it.
			await expect(page.getByTestId('refresh-analysis-pop').locator('.pop-log')).toContainText(line, {
				timeout: 2_000
			});
			await release();
			await page.unroute(matcher);
			await expect(page.getByText(/Refresh (done|failed):/).first()).toBeVisible({ timeout: 30_000 });
			await btn.press('Escape');
			await expectPollingStops(page, request, wire);
			return;
		}
		throw new Error('no attempt saw the engine log grow within a running refresh');
	});

	test('a status GET sent before the POST and answered after it does not undo the run', async ({ page, request }) => {
		await sweepRunsCli(request);
		await page.goto('/performance');
		const wire = watchWire(page);
		const btn = page.getByTestId('refresh-analysis');
		await expect(btn).toBeVisible({ timeout: 30_000 });
		await btn.focus();
		await waitIdle(request);
		// The popover's first status GET is sent to the real engine at once and
		// its real answer kept; the POST is held until that answer exists, so the
		// engine really answered the GET before the run started. The answer is
		// delivered to the page, unchanged, only after the POST has settled.
		const post = await holdRefreshPost(page);
		let gotStale = (): void => {};
		const staleReady = new Promise<void>((r) => (gotStale = r));
		let deliver = (): void => {};
		const delivered = new Promise<void>((r) => (deliver = r));
		let fulfilled = (): void => {};
		const staleDelivered = new Promise<void>((r) => (fulfilled = r));
		let staleBody: { running?: boolean } = {};
		let first = true;
		await page.route(
			(url) => url.pathname === STATUS,
			async (route) => {
				if (!first) return route.continue();
				first = false;
				const response = await route.fetch();
				staleBody = (await response.json()) as { running?: boolean };
				gotStale();
				await delivered;
				await route.fulfill({ response });
				fulfilled();
			}
		);
		await btn.press('Enter');
		await staleReady;
		expect(staleBody.running, 'the held answer is the pre-start idle one').toBe(false);
		const answered = page.waitForResponse(
			(r) => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/v1/ingest/refresh'
		);
		post.release();
		const started = (await (await answered).json()) as { running: boolean };
		expect(started.running, 'the POST started a run that is still going').toBe(true);
		// Dismissed with the run going: only the run keeps the polling alive now.
		await btn.press('Escape');
		await expect(page.getByTestId('refresh-analysis-pop')).toBeHidden();
		deliver();
		await staleDelivered;
		// Still followed: the page polls again after the stale answer landed. Had
		// it put the idle status back, the dismissed popover's timer would have
		// stopped right there and nothing would poll. (A done toast alone cannot
		// tell: the stale snapshot of the previous run can raise one too.)
		const afterStale = wire.status.length;
		await expect.poll(() => wire.status.length, { timeout: 5_000 }).toBeGreaterThan(afterStale);
		await expect(page.getByText(/Refresh (done|failed):/).first()).toBeVisible({ timeout: 30_000 });
		await expectPollingStops(page, request, wire);
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
