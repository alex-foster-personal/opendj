/**
 * AGENT-03: the order poll only ever asks a question the engine can answer.
 *
 * WHY THIS FILE EXISTS. The first cut of this poll fired
 * `GET /api/v1/commands/next` the instant /performance mounted, in the same
 * tick as its own first UI-mirror PUT. The engine gates every /commands route
 * on having a performance page ON RECORD, which only that PUT establishes, so
 * the poll routinely lost its own race and got the documented 409 back. It
 * threw, and the unhandled rejection landed in the client error log and failed
 * `tests/e2e/setup-entry-points.spec.ts` on every run.
 *
 * Catching the 409 would NOT have fixed it. The browser logs every non-2xx
 * fetch as a console error before app code ever sees the Response - the same
 * reason that spec has to filter a legitimate 401 and a legitimate 502 by hand.
 * The only fix is not to ask, so the behavior under test is an ABSENCE.
 *
 * [if] the poll runs before a mirror publish has been accepted [then ⛔] it
 *   asks anyway, and the 409 it earns fails the e2e client-error gate.
 * [if] a 409 arrives mid-session [then ⛔] the loop dies, and orders stop
 *   forever on a page that is about to re-register a second later.
 * [if] a 409 does not stand the poll down [then ⛔] it re-asks 20x/s and turns
 *   one console error into hundreds.
 * [if] any OTHER non-2xx is absorbed [then ⛔] a real broken route is silent.
 * [if] the uninstall does not stop the loop [then ⛔] it outlives /performance
 *   and polls an engine the route has already told the page is gone.
 *
 * AGENT-19 (Mon 5 Oct 2026 soak: a hidden leader took ~11 min to run one play):
 * [if] the claim is not a long poll, or the loop arms a timer between a held
 *   answer or an executed order and the next claim [then ⛔] a hidden tab, whose
 *   timers the browser throttles to 1 s or 60 s, sits on posted orders.
 * [if] an engine that ignores wait_ms is not reported [then ⛔] the throttled
 *   fallback is silent.
 */
import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const orders = await loadTypeScriptModule('src/lib/rb/agent-orders.ts');

const NEXT = orders.NEXT_ORDER_URL;

let realFetch;
let realConsoleInfo;
let calls;
let respond;
let infos;

/** A page registration whose flag the test drives, exactly as ui-mirror does. */
function registration(initial) {
	let registered = initial;
	let waiters = [];
	return {
		isRegistered: () => registered,
		whenRegistered: () =>
			registered ? Promise.resolve() : new Promise((resolve) => waiters.push(resolve)),
		forget: () => {
			registered = false;
		},
		reregister: () => {
			registered = true;
			for (const resolve of waiters) resolve();
			waiters = [];
		}
	};
}

function json(status, body, extraHeaders = {}) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json', ...extraHeaders }
	});
}

/** An empty answer the engine HELD for the whole long-poll window (AGENT-19). */
const heldEmpty = () => json(200, null, { [orders.ORDER_WAIT_HEADER]: String(orders.ORDER_LONG_POLL_MS) });

/** Long enough for several 50ms poll turns, short enough to stay a unit test. */
const settle = () => new Promise((resolve) => setTimeout(resolve, 220));

/** A network round trip: yields a macrotask WITHOUT using setTimeout, so a test can
 * count the timers the poll itself arms (a hidden tab throttles exactly those). */
const networkHop = () => new Promise((resolve) => setImmediate(resolve));

beforeEach(() => {
	realFetch = globalThis.fetch;
	realConsoleInfo = console.info;
	calls = [];
	infos = [];
	respond = heldEmpty;
	globalThis.fetch = async (url) => {
		calls.push(String(url));
		await networkHop();
		return respond(String(url));
	};
	console.info = (message) => infos.push(String(message));
});

afterEach(() => {
	globalThis.fetch = realFetch;
	console.info = realConsoleInfo;
});

test('it asks nothing at all until the engine has this page on record', async () => {
	const page = registration(false);
	let running = true;
	const loop = orders.pollAgentOrders(page, () => {}, () => running);

	await settle();
	assert.deepEqual(calls, [], 'an unregistered page must issue ZERO order polls');

	page.reregister();
	await settle();
	running = false;
	await loop;

	assert.ok(calls.length > 0, 'registration is what starts the poll, nothing else');
	assert.ok(
		calls.every((url) => url === NEXT),
		`the poll asks only for the next order, saw ${JSON.stringify(calls)}`
	);
});

test('a 409 stands the poll down as an expected state, and never throws', async () => {
	const page = registration(true);
	respond = () => json(409, { client_open: false });
	let running = true;
	const loop = orders.pollAgentOrders(page, () => {}, () => running);

	await settle();
	const afterConflict = calls.length;
	assert.equal(afterConflict, 1, 'one 409 is enough: the poll stops re-asking');
	assert.equal(page.isRegistered(), false, 'the 409 forgets the registration');
	assert.equal(
		infos.filter((line) => line.includes('agent orders paused')).length,
		1,
		'the state is logged at info, where the client error gate does not read'
	);

	// The publisher re-registers on its next accepted PUT: orders resume.
	respond = heldEmpty;
	page.reregister();
	await settle();
	running = false;
	await loop;

	assert.ok(calls.length > afterConflict, 'a 409 pauses the loop, it does not kill it');
});

test('any other non-2xx is raised, never absorbed', async () => {
	const page = registration(true);
	respond = () => json(500, { detail: 'broker exploded' });
	let running = true;

	await assert.rejects(
		() => orders.pollAgentOrders(page, () => {}, () => running),
		/agent order poll failed: 500/,
		'a 500 is a real defect and must reach the client error log'
	);
	running = false;
});

test('an unreachable engine does not kill the loop or reject', async () => {
	const page = registration(true);
	respond = () => {
		throw new TypeError('Load failed');
	};
	let running = true;
	const loop = orders.pollAgentOrders(page, () => {}, () => running);

	await settle();
	running = false;
	await loop;
	assert.ok(calls.length > 0, 'the poll keeps asking after a fetch TypeError');
});

test('uninstalling stops the loop, so it cannot outlive /performance', async () => {
	const page = registration(true);
	const uninstall = orders.installAgentOrderPoll(page, () => {});

	await settle();
	assert.ok(calls.length > 0, 'the installed poll runs while the route is mounted');

	uninstall();
	await settle();
	const parked = calls.length;
	await settle();
	assert.equal(calls.length, parked, 'nothing polls after uninstall');
});

// ------------------------------------------------------------ AGENT-19 ---

/** Count every timer the poll arms while `body` runs. A hidden tab throttles
 * exactly these, so the long-poll path must arm none. */
async function countingTimers(body) {
	const realSetTimeout = globalThis.setTimeout;
	const armed = [];
	globalThis.setTimeout = (handler, ms, ...rest) => {
		armed.push(ms);
		return realSetTimeout(handler, ms, ...rest);
	};
	try {
		await body();
	} finally {
		globalThis.setTimeout = realSetTimeout;
	}
	return armed;
}

/** Turn the event loop with setImmediate only, so no timer is armed by the wait. */
async function hops(n) {
	for (let i = 0; i < n; i += 1) await networkHop();
}

test('the claim is a long poll and re-asks at once after a held empty answer', async () => {
	const page = registration(true);
	let running = true;
	let loop;
	const armed = await countingTimers(async () => {
		loop = orders.pollAgentOrders(page, () => {}, () => running);
		await hops(40);
		running = false;
		await loop;
	});
	assert.ok(calls.length >= 5, `several held claims ran back to back, saw ${calls.length}`);
	assert.ok(
		calls.every((url) => url === `/api/v1/commands/next?wait_ms=${orders.ORDER_LONG_POLL_MS}`),
		`every claim asks the engine to hold it, saw ${JSON.stringify(calls.slice(0, 3))}`
	);
	assert.deepEqual(armed, [], 'no timer between held claims: a hidden tab would throttle it');
});

test('after an order runs, the next claim goes out with no timer in between', async () => {
	const page = registration(true);
	let served = false;
	const resultPosts = [];
	respond = (url) => {
		if (url.endsWith('/result')) {
			resultPosts.push(url);
			return json(202, { accepted: true });
		}
		if (!served) {
			served = true;
			// No performance IPC is installed under node, so executing it fails;
			// the loop must still report that result and go straight back.
			return json(200, { id: 'o1', kind: 'single', payload: { type: 'play', deck: 1, playing: true } });
		}
		return heldEmpty();
	};
	let running = true;
	let republished = 0;
	let loop;
	const armed = await countingTimers(async () => {
		loop = orders.pollAgentOrders(page, () => (republished += 1), () => running);
		await hops(40);
		running = false;
		await loop;
	});
	assert.deepEqual(resultPosts, ['/api/v1/commands/o1/result'], 'the order result is posted once');
	assert.equal(republished, 1, 'the mirror is republished after the order');
	const afterResult = calls.indexOf('/api/v1/commands/o1/result');
	assert.equal(calls[afterResult + 1], NEXT, 'the next request after the result is the next claim');
	assert.deepEqual(armed, [], 'no timer between the result and the next claim');
});

test('an engine that ignores wait_ms is reported once and falls back to the timer poll', async () => {
	const page = registration(true);
	respond = () => json(200, null);
	const errors = [];
	const realConsoleError = console.error;
	console.error = (message) => errors.push(String(message));
	let running = true;
	let loop;
	let armed;
	try {
		armed = await countingTimers(async () => {
			loop = orders.pollAgentOrders(page, () => {}, () => running);
			await settle();
			running = false;
			await loop;
		});
	} finally {
		console.error = realConsoleError;
	}
	assert.ok(calls.length >= 2, 'the fallback keeps polling');
	assert.equal(
		errors.filter((line) => line.includes('did not hold')).length,
		1,
		'one error per transition, not one per poll'
	);
	assert.ok(armed.includes(50), 'the fallback is the documented 50 ms timer poll');
});

test('with every timer frozen, as in a hidden tab, registration still starts the poll at once', async () => {
	const page = registration(false);
	const realSetTimeout = globalThis.setTimeout;
	// A hidden tab after five minutes: a timer may not fire for a minute. Model
	// the worst case, a timer that never fires, so only non-timer wake-ups count.
	globalThis.setTimeout = () => 0;
	let running = true;
	let loop;
	try {
		loop = orders.pollAgentOrders(page, () => {}, () => running);
		await hops(5);
		assert.deepEqual(calls, [], 'still unregistered: no claim');
		page.reregister();
		await hops(20);
		assert.ok(calls.length >= 2, `registration alone must start held claims, saw ${calls.length}`);
		assert.ok(calls.every((url) => url === NEXT));
	} finally {
		running = false;
		globalThis.setTimeout = realSetTimeout;
	}
	page.forget();
	page.reregister();
	await loop;
});
