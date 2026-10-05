/**
 * AGENT-19: an agent order reaches a HIDDEN leader tab in under 2 s.
 *
 * Mon 5 Oct 2026 soak: the preview tab went `visibilityState === 'hidden'`, its
 * 50 ms `setTimeout` claim loop was throttled by Chrome (1 s, then one wake-up
 * per minute after five hidden minutes), and one agent `play` took ~11 min.
 * The claim is now a long poll the engine holds open, and a network response is
 * not throttled.
 *
 * NOTHING HERE FAKES VISIBILITY. Per reload-countdown-browser.spec.ts the
 * repository forbids monkeypatching `document.visibilityState`, and headless or
 * Xvfb Chromium never reports hidden. So this runs headed on macOS only, where
 * minimizing the real window makes the OS hide it, and Chrome's own intensive
 * wake-up throttling is engaged after 10 s instead of 5 min by a Chrome feature
 * parameter (the throttle itself is the browser's, unmodified). Elsewhere the
 * capability is unavailable and the spec says so with a skip, never a pass.
 *
 * [if] an order posted to the hidden leader takes 2 s or more [then ⛔] agents
 *   cannot drive a backgrounded app.
 * [if] the mutation control (claim stripped of wait_ms, so the old timer poll
 *   runs) does NOT exceed 2 s [then ⛔] this harness is not measuring throttling,
 *   and the first test proves nothing.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

const BOUND_MS = 2_000;
const ORDERS = 5;
const INTENSIVE_GRACE_S = 10;

test.skip(
	process.platform !== 'darwin',
	'AGENT-19 needs a real OS window to hide; headless and Xvfb Chromium stay visible'
);
test.use({
	headless: false,
	launchOptions: {
		args: [`--enable-features=IntensiveWakeUpThrottling:grace_period_seconds/${INTENSIVE_GRACE_S}`]
	}
});

/** A harmless order the fixture page can always run: channel 1 fader to 0.5. */
const ORDER = { single: { type: 'fader', deck: 1, value: 0.5 } };

async function openLeader(page: Page): Promise<void> {
	await page.goto('/performance', { waitUntil: 'domcontentloaded' });
	await expect
		.poll(() => page.evaluate(() => window.musicDjToolsPerformance?.version ?? null), { timeout: 60_000 })
		.toBe(1);
	await expect
		.poll(
			() =>
				page.evaluate(async () => {
					const response = await fetch('/api/v1/state/ui-mirror/lease');
					return response.ok ? ((await response.json()) as { held: boolean }).held : false;
				}),
			{ timeout: 30_000, message: 'the page must hold the mirror lease (it is the leader)' }
		)
		.toBe(true);
}

/** Minimize the real window; returns false when the OS never hides the page. */
async function hide(page: Page): Promise<boolean> {
	const cdp = await page.context().newCDPSession(page);
	const { windowId } = await cdp.send('Browser.getWindowForTarget');
	await cdp.send('Browser.setWindowBounds', { windowId, bounds: { windowState: 'minimized' } });
	for (let i = 0; i < 50; i += 1) {
		if ((await page.evaluate(() => document.visibilityState)) === 'hidden') return true;
		await new Promise((resolve) => setTimeout(resolve, 100));
	}
	return false;
}

async function timedOrder(request: APIRequestContext, timeoutMs: number): Promise<number> {
	const started = Date.now();
	const response = await request.post('/api/v1/commands', { data: ORDER, timeout: timeoutMs });
	const elapsed = Date.now() - started;
	expect(response.status(), await response.text()).toBe(200);
	const body = (await response.json()) as { steps: Array<{ status: string; error?: string }> };
	expect(body.steps).toEqual([{ status: 'succeeded' }]);
	return elapsed;
}

async function hiddenLeader(page: Page): Promise<void> {
	await openLeader(page);
	test.skip(!(await hide(page)), 'the OS did not hide the minimized window: capability unavailable');
	// Let the page sit hidden past Chrome's intensive-throttling grace period.
	await new Promise((resolve) => setTimeout(resolve, (INTENSIVE_GRACE_S + 3) * 1000));
	expect(await page.evaluate(() => document.visibilityState)).toBe('hidden');
}

test('a hidden leader executes each agent order in under 2 s', async ({ page, request }) => {
	test.setTimeout(180_000);
	await hiddenLeader(page);
	const latencies: number[] = [];
	for (let i = 0; i < ORDERS; i += 1) {
		latencies.push(await timedOrder(request, 30_000));
		// Space the orders so each one meets an idle, held claim.
		await new Promise((resolve) => setTimeout(resolve, 1_500));
	}
	console.log(`AGENT-19 hidden-tab order latency ms: ${JSON.stringify(latencies)}`);
	expect(Math.max(...latencies), `latencies ${JSON.stringify(latencies)}`).toBeLessThan(BOUND_MS);
});

test('mutation control: without the long poll the hidden leader misses the bound', async ({
	page,
	request
}) => {
	test.setTimeout(300_000);
	// Strip wait_ms at the network layer: the engine answers at once without the
	// hold header, so the page falls back to its 50 ms timer poll, which is
	// exactly the pre-AGENT-19 loop. Nothing in the page is modified.
	await page.route('**/api/v1/commands/next*', (route) => {
		const url = new URL(route.request().url());
		url.searchParams.delete('wait_ms');
		return route.continue({ url: url.toString() });
	});
	await hiddenLeader(page);
	const latencies: number[] = [];
	for (let i = 0; i < 3; i += 1) {
		latencies.push(await timedOrder(request, 75_000));
		if (latencies[i] >= BOUND_MS) break;
	}
	console.log(`AGENT-19 mutation-control latency ms: ${JSON.stringify(latencies)}`);
	expect(Math.max(...latencies), `latencies ${JSON.stringify(latencies)}`).toBeGreaterThanOrEqual(BOUND_MS);
});
