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
 */
import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const NEXT = '/api/v1/commands/next';

const orders = await loadTypeScriptModule('src/lib/rb/agent-orders.ts');

let realFetch;
let realConsoleInfo;
let calls;
let respond;
let infos;

/** A page registration whose flag the test drives, exactly as ui-mirror does. */
function registration(initial) {
	let registered = initial;
	return {
		isRegistered: () => registered,
		forget: () => {
			registered = false;
		},
		reregister: () => {
			registered = true;
		}
	};
}

function json(status, body) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

/** Long enough for several 50ms poll turns, short enough to stay a unit test. */
const settle = () => new Promise((resolve) => setTimeout(resolve, 220));

beforeEach(() => {
	realFetch = globalThis.fetch;
	realConsoleInfo = console.info;
	calls = [];
	infos = [];
	respond = () => json(200, null);
	globalThis.fetch = async (url) => {
		calls.push(String(url));
		return respond();
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
	respond = () => json(200, null);
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
