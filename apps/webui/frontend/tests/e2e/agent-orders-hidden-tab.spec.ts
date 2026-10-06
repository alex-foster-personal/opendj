/**
 * AGENT-19: agent orders reach a HIDDEN leader tab promptly.
 *
 * A hidden tab's timers are throttled by Chrome (aligned to 1 s, and a timer
 * chain to one wake-up per minute after five hidden minutes). The order claim is
 * now a long poll the engine holds open, and a network response is not
 * throttled; a page waiting to register waits on the publisher, not on a timer.
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
 * A control inside the test proves the page under test is really throttled.
 * Off macOS there is no window to hide and the spec says so with a skip.
 *
 * WHY A MEDIAN, NOT A MAXIMUM. A hidden Chrome renderer on macOS also runs its
 * JavaScript far slower: a fixed busy loop measured 51-57 ms visible and
 * 283-3835 ms hidden on demon-llama (Tue 6 Oct 2026), with or without
 * --disable-renderer-backgrounding. So the per-order work itself (mirror delta,
 * the republish) has a CPU-bound tail of seconds on a loaded host that no claim
 * mechanism can remove.
 *
 * [if] the hidden page is not timer-throttled [then ⛔] the harness measures
 *   nothing, and the latency assertion proves nothing.
 * [if] the median order posted to the hidden leader takes 2 s or more, or any
 *   takes 30 s [then ⛔] agents cannot drive a backgrounded app.
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
		// Bounded, so a CDP call that never answers fails by name instead of
		// silently eating the whole test timeout.
		const reply = await new Promise<Record<string, unknown>>((resolve, reject) => {
			const timer = setTimeout(() => reject(new Error(`CDP ${method} gave no reply in 30 s`)), 30_000);
			this.pending.set(id, (message) => {
				clearTimeout(timer);
				resolve(message);
			});
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
	// A previous test's page may still hold the 10 s lease. Opening under it
	// makes this page a lease-blocked follower, and a hidden, silent follower
	// never re-claims (AGENT-18), so start only once the lease is free.
	await expect
		.poll(async () => ((await (await request.get('/api/v1/state/ui-mirror/lease')).json()) as { held: boolean }).held, {
			timeout: 20_000,
			message: 'the previous holder lease must lapse first'
		})
		.toBe(false);
	await page.send('Page.navigate', { url: `${baseURL}/performance` });
	await expect
		.poll(() => page.evaluate<number | null>('window.musicDjToolsPerformance?.version ?? null'), {
			timeout: 90_000
		})
		.toBe(1);
	// The lease holder must be THIS page's current mirror client, read twice
	// 3 s apart, so a page that reloaded (new client id) is not mistaken for it.
	const holdsLease = async (): Promise<boolean> => {
		const lease = (await (await request.get('/api/v1/state/ui-mirror/lease')).json()) as {
			held: boolean;
			holder: string | null;
		};
		const mirror = (await (await request.get('/api/v1/state/ui-mirror')).json()) as { client_id?: string };
		return lease.held && lease.holder === mirror.client_id;
	};
	await expect
		.poll(holdsLease, { timeout: 30_000, message: 'the page must hold the mirror lease (it is the leader)' })
		.toBe(true);
	await sleep(3_000);
	expect(await holdsLease(), 'leadership must be stable before hiding').toBe(true);
	return { page, browser, profile };
}

async function hide(leader: HiddenLeader): Promise<void> {
	const { windowId } = (await leader.page.send('Browser.getWindowForTarget')) as { windowId: number };
	// The OS occasionally ignores one minimize request (seen once in four runs on
	// demon-llama), so it is asked twice before the capability is called absent.
	let hidden = false;
	for (let attempt = 0; attempt < 2 && !hidden; attempt += 1) {
		await leader.page.send('Browser.setWindowBounds', { windowId, bounds: { windowState: 'minimized' } });
		for (let i = 0; i < 100 && !hidden; i += 1) {
			hidden = (await leader.page.evaluate<string>('document.visibilityState')) === 'hidden';
			if (!hidden) await sleep(100);
		}
	}
	test.skip(!hidden, 'the OS did not hide the minimized window twice: capability unavailable on this host now');
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
		// Control: the page under test really is timer-throttled. Three chained
		// 50 ms timers take about 150 ms in a visible tab and at least 1 s here.
		const chainMs = await leader.page.evaluate<number>(
			'(async () => { const s = performance.now(); for (let i = 0; i < 3; i += 1) await new Promise((r) => setTimeout(r, 50)); return performance.now() - s; })()'
		);
		expect(chainMs, 'control: the hidden page must be timer-throttled').toBeGreaterThanOrEqual(900);
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
