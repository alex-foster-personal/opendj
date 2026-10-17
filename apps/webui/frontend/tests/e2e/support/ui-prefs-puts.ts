/**
 * The page's own PUTs to /api/v1/ui-prefs: what it sent, what landed, and what
 * is still in flight.
 *
 * Root specs share one engine and one ui-prefs.json, and many of them put the
 * disk back in an afterEach hook. That hook runs while the test's page is still
 * open (Playwright tears the page fixture down after afterEach), so a PUT the
 * page already SENT can still be on its way through the vite proxy. If it lands
 * between the hook's reset PUT and its read-back, the read returns the page's
 * value, and if it lands after, the next spec inherits it.
 *
 * `settle()` ends the page's writes definitively instead of inferring the end
 * from a quiet period: the page serializes its writes (`disk-write-chain.ts`),
 * so a renderer paused after one response can send the next queued PUT at any
 * later moment. It first routes every further ui-prefs PUT to an abort inside
 * the browser, so none started from then on can reach the engine (a PUT the
 * close aborted after the proxy forwarded it could still land after the reset).
 * It then waits until every PUT that did go out has a server answer, closes the
 * page, and drains anything observed up to the close. It fails if any PUT that
 * went out came back non-2xx, or ended without a server answer, since then
 * nobody knows whether or when it lands: a write that did not land is a test
 * failure, never something cleanup quietly papers over.
 *
 * A PUT the page starts after the route is in place but before the close can
 * still reach the close unanswered, with its route handler not yet run (seen
 * in e2e run 38980579517: `{"midi_enabled":false}`, the WebMIDI denial's own
 * write, "no server answer before the page closed"). Routing runs before a
 * request is sent, so such a PUT never left the browser either: it counts as
 * blocked, like one the route did abort. A PUT first seen BEFORE the route is
 * in place gets no such pass, since it may already be on the wire.
 */
import { expect, type Page, type Request } from '@playwright/test';

export interface UiPrefsPutWatch {
	/** Every ui-prefs PUT body the page sent, in the order it sent them. */
	readonly sent: Record<string, unknown>[];
	/** The bodies whose PUT came back 2xx, so the engine wrote them to disk. */
	readonly landed: Record<string, unknown>[];
	/** PUTs sent but not yet answered (finished or failed). */
	readonly inFlight: Set<Request>;
	/** PUTs that went out and came back non-2xx or with no server answer. */
	readonly failed: string[];
	/** Block further PUTs, wait for every one that went out, close; reject if any did not land. */
	settle(): Promise<void>;
}

function uiPrefsPutBody(r: Request): Record<string, unknown> | null {
	if (r.method() !== 'PUT' || !new URL(r.url()).pathname.endsWith('/api/v1/ui-prefs')) return null;
	const body = r.postDataJSON() as unknown;
	return body !== null && typeof body === 'object' ? (body as Record<string, unknown>) : null;
}

export function watchUiPrefsPuts(page: Page): UiPrefsPutWatch {
	const sent: Record<string, unknown>[] = [];
	const landed: Record<string, unknown>[] = [];
	const inFlight = new Set<Request>();
	const failed: string[] = [];
	// PUTs settle()'s route aborted inside the browser: they never reached the
	// engine, so they are the one unanswered outcome that is known not to land.
	const blocked = new Set<Request>();
	// PUTs first seen once settle()'s route was in place: every one of them has
	// to pass that route before it is sent, so if it ends unanswered it never
	// reached the engine, whether or not the route handler got to run.
	const afterRoute = new Set<Request>();
	let routeInPlace = false;
	const record = (r: Request, status: number | null, why: string | null) => {
		if (!inFlight.delete(r)) return;
		const body = uiPrefsPutBody(r);
		if (status !== null && status >= 200 && status < 300) {
			if (body !== null) landed.push(body);
		} else if (status !== null || !(blocked.has(r) || afterRoute.has(r))) {
			failed.push(`${JSON.stringify(body)}: ${status !== null ? `HTTP ${status}` : (why ?? 'no response')}`);
		}
	};
	page.on('request', (r) => {
		const body = uiPrefsPutBody(r);
		if (body === null) return;
		sent.push(body);
		inFlight.add(r);
		if (routeInPlace) afterRoute.add(r);
	});
	page.on('requestfinished', async (r) => {
		if (!inFlight.has(r)) return;
		const res = await r.response();
		record(r, res?.status() ?? null, null);
	});
	page.on('requestfailed', (r) => record(r, null, r.failure()?.errorText ?? 'request failed'));
	return {
		sent,
		landed,
		inFlight,
		failed,
		settle: async () => {
			// From here on no ui-prefs PUT can leave the browser. The route runs
			// before a request is sent, so an abort there is known not to land.
			await page.route(
				(url) => url.pathname.endsWith('/api/v1/ui-prefs'),
				(route) => {
					if (route.request().method() !== 'PUT') return route.fallback();
					blocked.add(route.request());
					return route.abort('aborted');
				}
			);
			routeInPlace = true;
			// Every PUT that went out before the route gets its server answer.
			await expect
				.poll(() => inFlight.size, {
					timeout: 15_000,
					intervals: [100],
					message: 'the page still has ui-prefs PUTs in flight'
				})
				.toBe(0);
			await page.close();
			// Drain whatever was observed up to the close, snapshotted AFTER it, on
			// the request itself, since a closed page emits no more events. Only a
			// PUT that never left the browser (blocked, or started behind the
			// route) may end unanswered; any other one fails below.
			for (const r of [...inFlight]) {
				const res = await r.response().catch(() => null);
				record(r, res?.status() ?? null, 'no server answer before the page closed');
			}
			expect(failed, 'ui-prefs PUTs the page sent that did not land').toEqual([]);
		}
	};
}
