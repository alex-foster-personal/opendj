import assert from 'node:assert/strict';
import { after, afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * P0 (audio-never-cuts-out-under-thrash, defect D4): a perf event nobody can
 * read from outside the browser is not a record of anything.
 *
 * `recordPerfEvent` writes to console, an in-memory ring, and LocalStorage
 * `mdt.perfEventLog`. That is where it stops. `reportClientError`
 * (src/lib/client-error-reporting.ts) POSTs to `/api/v1/client-errors`, has a
 * durable retry queue, and is already wired into `hooks.client.ts` and
 * `stores.svelte.ts` - but NO perf event of any severity is ever handed to it.
 *
 * So on Wed 2 Sep 2026 the only durable trace of a 24-minute audio outage
 * would have lived in the LocalStorage of the browser profile that dropped
 * out: the one place nobody looks mid-set, and the one place a profile reset
 * destroys. The whole point of a hardening instrument is that somebody other
 * than the person in the room can see it afterwards.
 *
 * WHY THE ASSERTION IS ON THE POST rather than on a spy: `perf-event-log.ts`
 * takes no injectable reporter today, so there is nothing to inject into. The
 * network boundary is the contract that actually matters and it is the one
 * thing a fix cannot fake - `reportClientError` is exercised here exactly as
 * client-error-reporting.test.mjs exercises it, through a fake `fetch`.
 *
 * Regression lines:
 * - if an error-severity audio event never leaves the browser then the only
 *   record of an outage dies with the profile that suffered it
 * - if info/warn rows are forwarded too then one PitchFader drag (which emits
 *   a transport-schedule row per pointermove) floods /api/v1/client-errors
 */

const API_BASE = 'https://perf-escalation.example.test';

/** The three kinds this P0 is about. All three must escalate at error severity. */
const ESCALATING_KINDS = ['xrun', 'audio-context', 'silent-while-playing'];

let perfLog;
let originalFetch;
let posted;

function defineGlobal(name, value) {
	Object.defineProperty(globalThis, name, {
		value,
		configurable: true,
		writable: true,
		enumerable: true
	});
}

function makeLocalStorage() {
	const map = new Map();
	return {
		getItem: (key) => (map.has(key) ? map.get(key) : null),
		setItem: (key, value) => map.set(key, String(value)),
		removeItem: (key) => map.delete(key)
	};
}

function installBrowserGlobals() {
	const store = makeLocalStorage();
	defineGlobal('window', {
		location: { href: 'https://app.example.test/performance' },
		isSecureContext: true,
		localStorage: store,
		addEventListener: () => {}
	});
	defineGlobal('localStorage', store);
	defineGlobal('navigator', { userAgent: 'perf-escalation-test-agent' });
	defineGlobal('crypto', { randomUUID: () => `perf-${Math.random().toString(36).slice(2)}` });
	defineGlobal('AudioWorkletNode', function AudioWorkletNode() {});
}

/** Every POST body this test saw, in order. */
function captureFetch() {
	posted = [];
	globalThis.fetch = async (input) => {
		posted.push({ url: input.url, body: await input.clone().json() });
		return new Response(JSON.stringify({ event_id: 'e', stored: true }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
}

/**
 * Wait for a POST that matches, or give up.
 *
 * Bounded rather than open-ended: the whole point of these tests today is that
 * the POST never comes, and a test that hangs is not a test that reports.
 */
async function waitForPost(predicate, budgetMs = 600) {
	const deadline = Date.now() + budgetMs;
	while (Date.now() < deadline) {
		const found = posted.find(predicate);
		if (found !== undefined) return found;
		await new Promise((resolve) => setTimeout(resolve, 10));
	}
	return null;
}

before(async () => {
	originalFetch = globalThis.fetch;
	installBrowserGlobals();
	captureFetch();
	perfLog = await loadTypeScriptModule('src/lib/rb/perf-event-log.ts', { viteApiBase: API_BASE });
});

afterEach(() => {
	installBrowserGlobals();
	captureFetch();
});

after(() => {
	globalThis.fetch = originalFetch;
});

//-----------------------------------------------------------------------------
// what must escalate
//-----------------------------------------------------------------------------

for (const kind of ESCALATING_KINDS) {
	test(`an error-severity ${kind} event reaches /api/v1/client-errors`, async () => {
		const message = `${kind} escalation probe ${Math.random().toString(36).slice(2)}`;
		perfLog.recordPerfEvent(kind, message, null, 'error');
		const hit = await waitForPost((post) => post.body.message.includes(message));
		assert.ok(
			hit !== null,
			`if an error-severity '${kind}' perf event is not forwarded to reportClientError ` +
				'then broken - the row exists only in the LocalStorage of the browser profile ' +
				'that failed, so a 24-minute audio outage leaves nothing anybody else can read ' +
				`(POSTs seen: ${JSON.stringify(posted.map((p) => p.url))})`
		);
		assert.ok(
			hit.url.endsWith('/api/v1/client-errors'),
			`if the escalation goes anywhere other than /api/v1/client-errors then broken`
		);
		assert.ok(
			JSON.stringify(hit.body).includes(kind),
			`if the forwarded payload does not carry the perf kind ('${kind}') then broken - ` +
				'the engine cannot tell an audio dropout from any other client error'
		);
	});
}

//-----------------------------------------------------------------------------
// what must NOT escalate
//-----------------------------------------------------------------------------

test('CONTROL: warn and info severity rows stay local', async () => {
	// Passes today for the trivial reason that NOTHING escalates. It is here for
	// the fix: `transport-schedule` is appended once per _scheduleDeck and
	// PitchFader drives that from an unthrottled pointermove, so "forward every
	// perf event" would put ~40 POSTs on the wire per fader drag, during a set,
	// on the same main thread as the audio this P0 is trying to protect.
	perfLog.recordPerfEvent('transport-schedule', 'routine schedule row', 2, 'warn');
	perfLog.recordPerfEvent('deck-load', 'routine load row', 1, 'info');
	const leaked = await waitForPost(() => true, 200);
	assert.equal(
		leaked,
		null,
		'if a warn/info perf row is POSTed to the engine then broken - one PitchFader drag ' +
			`would flood /api/v1/client-errors (leaked: ${JSON.stringify(leaked)})`
	);
});

test('a sustained condition escalates once per window, not once per report', async () => {
	// The xrun sentinel reports every 2s for as long as the machine struggles,
	// so a twenty-minute incident is ~600 reports describing the same twenty
	// minutes. reportClientError's own dedupe cannot absorb them: it fingerprints
	// on the exact message, and these messages carry live numbers.
	const kind = 'presentation-clock-stalled';
	for (let report = 0; report < 25; report += 1) {
		perfLog.recordPerfEvent(kind, `stall report ${report}, worst gap ${report}ms`, null, 'error');
	}
	await waitForPost(() => true, 200);
	assert.equal(
		posted.length,
		1,
		`if a sustained condition POSTs ${posted.length} times then broken - one incident ` +
			'must not become one round trip per report, on the same main thread as the audio'
	);
});

test('CONTROL: the ring still records the row it escalated', async () => {
	// Escalation must be additive. If a fix routes the row to the engine INSTEAD
	// of the ring, __mdtPerfLog() and the toast correlation ids both go dark.
	const message = `ring retention probe ${Math.random().toString(36).slice(2)}`;
	perfLog.recordPerfEvent('xrun', message, null, 'error');
	assert.ok(
		perfLog.readPerfEvents().some((row) => row.message === message),
		'if escalating a row drops it from the local ring then broken - the ring is what ' +
			'__mdtPerfLog() and every toast correlation id read'
	);
});
