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
 * repository forbids monkeypatching `document.visibilityState`, and a page that
 * Playwright drives never goes hidden: it launches Chromium with
 * --disable-backgrounding-occluded-windows and friends and emulates focus on
 * every page it attaches to, connectOverCDP included (measured Tue 6 Oct 2026
 * on demon-llama: minimized, still 'visible', timers still 50 ms). So this spec
 * starts Playwright's own Chromium binary as a plain browser and speaks raw CDP
 * to it. On macOS, minimizing that real window makes the OS hide the page;
 * Chrome's intensive wake-up throttling is engaged after 10 s instead of 5 min
 * by a Chrome feature parameter (the throttle is the browser's, unmodified).
 * Measured the same day: hidden, 1 s timer alignment, then no timer for 15 s.
 * Off macOS there is no window to hide and the spec says so with a skip.
 *
 * WHY A MEDIAN, NOT A MAXIMUM. A hidden Chrome renderer on macOS also runs its
 * JavaScript far slower: a fixed busy loop measured 51-57 ms visible and
 * 283-3835 ms hidden on demon-llama (Tue 6 Oct 2026), with or without
 * --disable-renderer-backgrounding. So the per-order work itself (mirror delta,
 * the republish) has a CPU-bound tail of seconds on a loaded host that no claim
 * mechanism can remove. What the long poll removes is the TIMER wait, which is
 * unbounded (one minute per hop); the mutation control below measures exactly
 * that difference.
 *
 * [if] the median order posted to the hidden leader takes 2 s or more, or any
 *   takes 30 s [then ⛔] agents cannot drive a backgrounded app.
 * [if] the mutation control (claim stripped of wait_ms at the network layer, so
 *   the pre-AGENT-19 timer poll runs) does NOT exceed 2 s [then ⛔] this harness
 *   is not measuring throttling, and the first test proves nothing.
 */
import { spawn, type ChildProcess } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { chromium, expect, test, type APIRequestContext } from '@playwright/test';

const BOUND_MS = 2_000;
const ORDERS = 7;
const INTENSIVE_GRACE_S = 10;
/** A harmless order the fixture page can always run: channel 1 fader to 0.5. */
const ORDER = { single: { type: 'fader', deck: 1, value: 0.5 } };

test.skip(process.platform !== 'darwin', 'AGENT-19 needs a real OS window to hide; only macOS has one here');

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

/** One raw CDP page session over the browser's DevTools WebSocket. */
class RawPage {
	private nextId = 0;
	private readonly pending = new Map<number, (message: Record<string, unknown>) => void>();
	private readonly listeners = new Map<string, (params: Record<string, unknown>) => void>();

	private constructor(private readonly socket: WebSocket) {
		socket.onmessage = (event) => {
			const message = JSON.parse(String(event.data)) as Record<string, unknown>;
			if (typeof message.id === 'number') {
				this.pending.get(message.id)?.(message);
				this.pending.delete(message.id);
			} else if (typeof message.method === 'string') {
				this.listeners.get(message.method)?.(message.params as Record<string, unknown>);
			}
		};
	}

	static async open(url: string): Promise<RawPage> {
		const socket = new WebSocket(url);
		await new Promise<void>((resolve, reject) => {
			socket.onopen = () => resolve();
			socket.onerror = () => reject(new Error(`CDP socket ${url} failed to open`));
		});
		return new RawPage(socket);
	}

	async send(method: string, params: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
		this.nextId += 1;
		const id = this.nextId;
		const reply = await new Promise<Record<string, unknown>>((resolve) => {
			this.pending.set(id, resolve);
			this.socket.send(JSON.stringify({ id, method, params }));
		});
		if (reply.error !== undefined) throw new Error(`${method}: ${JSON.stringify(reply.error)}`);
		return reply.result as Record<string, unknown>;
	}

	on(method: string, listener: (params: Record<string, unknown>) => void): void {
		this.listeners.set(method, listener);
	}

	async evaluate<T>(expression: string): Promise<T> {
		const result = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
		return (result.result as { value: T }).value;
	}

	close(): void {
		this.socket.close();
	}
}

interface HiddenLeader {
	page: RawPage;
	browser: ChildProcess;
	profile: string;
}

async function launchPlainChromium(): Promise<{ browser: ChildProcess; profile: string; port: number }> {
	const profile = mkdtempSync(join(tmpdir(), 'agent19-'));
	const browser = spawn(
		chromium.executablePath(),
		[
			'--remote-debugging-port=0',
			`--user-data-dir=${profile}`,
			'--no-first-run',
			'--no-default-browser-check',
			'--autoplay-policy=no-user-gesture-required',
			`--enable-features=IntensiveWakeUpThrottling:grace_period_seconds/${INTENSIVE_GRACE_S}`,
			'about:blank'
		],
		{ stdio: 'ignore' }
	);
	for (let i = 0; i < 150; i += 1) {
		try {
			const [port] = readFileSync(join(profile, 'DevToolsActivePort'), 'utf8').split('\n');
			if (port) return { browser, profile, port: Number(port) };
		} catch {
			// Not written yet.
		}
		await sleep(100);
	}
	browser.kill();
	throw new Error('Chromium never wrote DevToolsActivePort');
}

async function openHiddenLeader(baseURL: string, request: APIRequestContext): Promise<HiddenLeader> {
	const { browser, profile, port } = await launchPlainChromium();
	const targets = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()) as Array<{
		type: string;
		webSocketDebuggerUrl: string;
	}>;
	const target = targets.find((candidate) => candidate.type === 'page');
	if (target === undefined) throw new Error('no page target in the plain Chromium');
	const page = await RawPage.open(target.webSocketDebuggerUrl);
	await page.send('Page.navigate', { url: `${baseURL}/performance` });
	await expect
		.poll(() => page.evaluate<number | null>('window.musicDjToolsPerformance?.version ?? null'), {
			timeout: 90_000
		})
		.toBe(1);
	await expect
		.poll(
			async () => ((await (await request.get('/api/v1/state/ui-mirror/lease')).json()) as { held: boolean }).held,
			{ timeout: 30_000, message: 'the page must hold the mirror lease (it is the leader)' }
		)
		.toBe(true);
	return { page, browser, profile };
}

async function hide(leader: HiddenLeader): Promise<void> {
	const { windowId } = (await leader.page.send('Browser.getWindowForTarget')) as { windowId: number };
	await leader.page.send('Browser.setWindowBounds', { windowId, bounds: { windowState: 'minimized' } });
	await expect
		.poll(() => leader.page.evaluate<string>('document.visibilityState'), { timeout: 10_000 })
		.toBe('hidden');
	// Sit hidden past Chrome's intensive-throttling grace period.
	await sleep((INTENSIVE_GRACE_S + 3) * 1000);
	expect(await leader.page.evaluate<string>('document.visibilityState')).toBe('hidden');
}

function close(leader: HiddenLeader | null): void {
	if (leader === null) return;
	leader.page.close();
	leader.browser.kill();
	rmSync(leader.profile, { recursive: true, force: true });
}

/** Latency of one order the page completed, or null when it did not answer in `timeoutMs`. */
async function timedOrderOrTimeout(request: APIRequestContext, timeoutMs: number): Promise<number | null> {
	try {
		return await timedOrder(request, timeoutMs);
	} catch (error) {
		if (error instanceof Error && error.name === 'TimeoutError') return null;
		throw error;
	}
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

test('a hidden leader executes agent orders with a median under 2 s and none past 30 s', async ({ request, baseURL }) => {
	test.setTimeout(240_000);
	let leader: HiddenLeader | null = null;
	try {
		leader = await openHiddenLeader(String(baseURL), request);
		await hide(leader);
		const latencies: number[] = [];
		for (let i = 0; i < ORDERS; i += 1) {
			latencies.push(await timedOrder(request, 30_000));
			await sleep(1_500);
		}
		console.log(`AGENT-19 hidden-tab order latency ms: ${JSON.stringify(latencies)}`);
		const median = [...latencies].sort((a, b) => a - b)[Math.floor(latencies.length / 2)];
		expect(median, `median of ${JSON.stringify(latencies)}`).toBeLessThan(BOUND_MS);
	} finally {
		close(leader);
	}
});

test('mutation control: without the long poll the hidden leader misses the bound', async ({
	request,
	baseURL
}) => {
	test.setTimeout(360_000);
	let leader: HiddenLeader | null = null;
	try {
		leader = await openHiddenLeader(String(baseURL), request);
		// Strip wait_ms at the network layer: the engine answers at once without
		// the hold header, so the page falls back to its 50 ms timer poll, which
		// is the pre-AGENT-19 loop. Nothing in the page is modified.
		const page = leader.page;
		page.on('Fetch.requestPaused', (params) => {
			const url = new URL(String((params.request as { url: string }).url));
			url.searchParams.delete('wait_ms');
			void page.send('Fetch.continueRequest', { requestId: params.requestId, url: url.toString() });
		});
		await page.send('Fetch.enable', { patterns: [{ urlPattern: '*/api/v1/commands/next*' }] });
		await hide(leader);
		// An order still unanswered at the timeout has missed the bound too; the
		// pre-AGENT-19 loop waits up to a minute per throttled hop. A post can land
		// just before an aligned wake-up (about 2 s in 60), so up to three are tried.
		const latencies: Array<number | null> = [];
		for (let i = 0; i < 3; i += 1) {
			const latency = await timedOrderOrTimeout(request, 20_000);
			latencies.push(latency);
			if (latency === null || latency >= BOUND_MS) break;
		}
		console.log(`AGENT-19 mutation-control latency ms (null = no answer in 20 s): ${JSON.stringify(latencies)}`);
		const last = latencies[latencies.length - 1];
		expect(last === null || last >= BOUND_MS, `latencies ${JSON.stringify(latencies)}`).toBe(true);
	} finally {
		close(leader);
	}
});
