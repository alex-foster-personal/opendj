/**
 * The four things a toast owes its reader, after an error toast reported a real
 * failure and could neither be held still, dismissed, nor reported.
 *
 *   1 HOVER HOLDS, AND RESETS. A pointer over a toast stops it fading, and
 *     leaving restarts the FULL delay rather than whatever was left of it.
 *   2 X DISMISSES. Every toast carries one.
 *   3 CLICK COPIES. Message, timestamp, machine, user, client, version, id.
 *   4 THE ID IS THE LOG'S ID. Not a display-only id minted at render time.
 *
 * Requirement 4 is the one that makes the other three worth anything, and it is
 * the one that fails invisibly: an id that looks like a correlation key and
 * matches nothing costs the reader a search before they learn it never worked.
 * So it is asserted against all three surfaces that carry it - the console line
 * somebody greps, the localStorage ring that survives the toast, and the
 * server-side report - rather than against the store's own bookkeeping.
 *
 * Regression lines:
 * - if a hovered toast still fades then reading a message costs you the message
 * - if leaving a hover resumes the remainder instead of restarting then a toast
 *   held to the last moment vanishes the instant you look away
 * - if the dismiss control loses its handler or its test hook then the stuck
 *   error banner is back
 * - if the copied id differs from the logged id then the id is decorative
 * - if the console line stops printing the id then the id only helps whoever
 *   already knew to open localStorage
 * - if the copied timestamp is measured separately from the ring row's then two
 *   readings of the same event disagree
 * - if pushToast stops reporting toast_id to the server then a remote failure
 *   cannot be tied to what the user saw
 * - if the clipboard failure is swallowed then a copy that did nothing looks
 *   like a copy that worked
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { afterEach, beforeEach, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://toast-behavior.example.test';
const PERF_KEY = 'mdt.perfEventLog';
const HOSTNAME = 'maintainer-macbook-air';

function read(relative) {
	return readFileSync(fileURLToPath(new URL(`../../${relative}`, import.meta.url)), 'utf8');
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

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

let store;
let clipboardWrites;
let consoleLines;
/** Index-aligned with consoleLines: which console method emitted each row. */
let consoleLevels;
let reportedBodies;
let originalFetch;
let originalConsole;

function install({ clipboard = 'ok', secure = true } = {}) {
	store = makeLocalStorage();
	clipboardWrites = [];
	consoleLines = [];
	consoleLevels = [];
	reportedBodies = [];

	const clipboardImpl =
		clipboard === 'ok'
			? { writeText: async (text) => void clipboardWrites.push(text) }
			: clipboard === 'reject'
				? {
						writeText: async () => {
							throw new Error('NotAllowedError: no user gesture');
						}
					}
				: undefined;

	defineGlobal('window', {
		location: {
			href: 'http://127.0.0.1:8585/performance?deck=2',
			pathname: '/performance'
		},
		isSecureContext: secure,
		localStorage: store,
		addEventListener: () => {}
	});
	defineGlobal('localStorage', store);
	defineGlobal('navigator', {
		userAgent:
			'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15',
		clipboard: clipboardImpl
	});
	defineGlobal('crypto', {
		randomUUID: () => `uuid-${Math.random()}`,
		getRandomValues: (array) => {
			for (let i = 0; i < array.length; i += 1) array[i] = Math.floor(Math.random() * 0xffffffff);
			return array;
		}
	});
	defineGlobal('AudioWorkletNode', function AudioWorkletNode() {});

	originalConsole = { info: console.info, warn: console.warn, error: console.error };
	for (const level of ['info', 'warn', 'error']) {
		console[level] = (...args) => {
			consoleLevels.push(level);
			consoleLines.push(args.join(' '));
		};
	}

	originalFetch = globalThis.fetch;
	globalThis.fetch = async (input) => {
		const url = typeof input === 'string' ? input : input.url;
		if (url.includes('/api/v1/settings')) {
			return new Response(
				JSON.stringify({
					groups: [
						{ group: 'network', items: [{ key: 'bind_host', value: '127.0.0.1' }, { key: 'hostname', value: HOSTNAME }] }
					]
				}),
				{ status: 200, headers: { 'content-type': 'application/json' } }
			);
		}
		if (url.includes('/api/v1/client-errors')) {
			reportedBodies.push(await input.clone().json());
			return new Response(JSON.stringify({ event_id: 'e1', stored: true }), {
				status: 200,
				headers: { 'content-type': 'application/json' }
			});
		}
		return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
	};
}

beforeEach(() => install());

afterEach(() => {
	globalThis.fetch = originalFetch;
	if (originalConsole) Object.assign(console, originalConsole);
	delete globalThis.window;
});

async function loadStores() {
	return loadTypeScriptModule('src/lib/stores.svelte.ts', { viteApiBase: API_BASE });
}

//-----------------------------------------------------------------------------
// 1. hover holds, and hover resets
//-----------------------------------------------------------------------------

test('a hovered toast does not fade', async () => {
	const stores = await loadStores();
	stores.pushToast('held open', 'info', 200);
	const { logId } = stores.toasts[0];

	await sleep(120);
	assert.equal(stores.holdToast(logId), true);
	await sleep(400); // long past the 200ms it would have died at

	assert.equal(
		stores.toasts.length,
		1,
		'a toast under the pointer must still be there; reading it must not cost it'
	);
	assert.equal(stores.toastTimerArmed(logId), false, 'no timer may be armed while held');
});

test('leaving a hover restarts the FULL delay rather than the remainder', async () => {
	const stores = await loadStores();
	stores.pushToast('reset me', 'info', 200);
	const { logId } = stores.toasts[0];

	// Hold with only ~20ms of the original 200ms left. If release resumed the
	// remainder it would fire ~20ms later; a reset fires ~200ms later. The two
	// outcomes are 180ms apart, which is what makes this discriminating.
	await sleep(180);
	stores.holdToast(logId);
	await sleep(300);
	assert.equal(stores.toasts.length, 1, 'still held');

	stores.releaseToast(logId);
	assert.equal(stores.toastTimerArmed(logId), true, 'releasing must re-arm a timer');
	await sleep(120);
	assert.equal(
		stores.toasts.length,
		1,
		'120ms after release the toast must still be there; a resumed 20ms remainder would ' +
			'have fired at 20ms, which is the bug this asserts against'
	);

	await sleep(220);
	assert.equal(stores.toasts.length, 0, 'and the restarted full delay must still expire');
});

test('an unhovered toast fades on its own, so the hold is doing the work', async () => {
	const stores = await loadStores();
	stores.pushToast('ordinary', 'info', 120);
	assert.equal(stores.toasts.length, 1);
	await sleep(400);
	assert.equal(stores.toasts.length, 0);
});

//-----------------------------------------------------------------------------
// 2. every toast carries an x
//-----------------------------------------------------------------------------

test('dismiss removes the toast and cancels its timer', async () => {
	const stores = await loadStores();
	stores.pushToast('dismiss me', 'error', 5000);
	const { logId } = stores.toasts[0];

	assert.equal(stores.dismissToast(logId), true);
	assert.equal(stores.toasts.length, 0);
	assert.equal(stores.toastTimerArmed(logId), false);
});

test('dismissing something that is not there reports false rather than lying', async () => {
	const stores = await loadStores();
	assert.equal(stores.dismissToast('t-nope-1'), false);
});

test('dismissing one toast leaves the others alone', async () => {
	const stores = await loadStores();
	stores.pushToast('first', 'info', 5000);
	stores.pushToast('second', 'info', 5000);
	stores.pushToast('third', 'info', 5000);
	const second = stores.toasts[1].logId;

	stores.dismissToast(second);
	assert.deepEqual(
		stores.toasts.map((t) => t.message),
		['first', 'third']
	);
});

const TOAST_STACK = read('src/lib/components/rb/ToastStack.svelte');

test('the markup gives every toast a dismiss control, a copy target and hover handlers', () => {
	// Source-shape, and only where unavoidable: this harness is node:test with
	// no component mount infra, the same reason capability-gating-markup.test.mjs
	// pins its facts this way. Everything testable by execution is above.
	assert.match(TOAST_STACK, /data-toast-dismiss=\{toast\.logId\}/, 'the x needs an agent hook');
	assert.match(TOAST_STACK, /onclick=\{\(event\) => \{\s*event\.stopPropagation\(\);\s*dismissToast\(toast\.logId\)/,
		'the x is nested inside the copy target, so without stopPropagation dismissing also copies');
	assert.match(TOAST_STACK, /onmouseenter=\{\(\) => holdToast\(toast\.logId\)\}/);
	assert.match(TOAST_STACK, /onmouseleave=\{\(\) => releaseToast\(toast\.logId\)\}/);
	assert.match(TOAST_STACK, /data-toast-copy=\{toast\.logId\}/);
	assert.match(TOAST_STACK, /aria-label=\{`Dismiss message \$\{toast\.logId\}`\}/);
});

test('a failed copy is rendered rather than swallowed', () => {
	assert.match(
		TOAST_STACK,
		/data-toast-copy-failed=\{toast\.logId\}/,
		'the failure must reach the screen; a silent catch is the bug'
	);
	assert.equal(
		/catch\s*\{\s*\}/.test(TOAST_STACK),
		false,
		'no empty catch may stand between a failed clipboard write and the user'
	);
});

//-----------------------------------------------------------------------------
// 3. clicking copies, with the environment
//-----------------------------------------------------------------------------

test('a copy carries the message, the timestamp, the id and the environment', async () => {
	const stores = await loadStores();
	stores.pushToast('Deck 2 could not be set as master', 'error', 5000);
	const toast = stores.toasts[0];
	await sleep(30); // let the prefetched hostname land

	const text = await stores.copyToast(toast.logId);

	assert.deepEqual(clipboardWrites, [text], 'the clipboard gets exactly what was returned');
	assert.match(text, new RegExp(`^id: ${toast.logId}$`, 'm'));
	assert.match(text, new RegExp(`^when: ${toast.createdAt.replace(/[.]/g, '\\.')}$`, 'm'));
	assert.match(text, /^message: Deck 2 could not be set as master$/m);
	assert.match(text, new RegExp(`^machine: ${HOSTNAME}$`, 'm'), 'machine comes from /settings');
	assert.match(text, /^user: signed out$/m, 'signed out is a fact, not a blank');
	assert.match(text, /^client: Safari 18\.1$/m, 'client AND version');
});

test('the copied page drops the query string it was reached with', async () => {
	const stores = await loadStores();
	stores.pushToast('note', 'info', 5000);
	await sleep(30);
	const text = await stores.copyToast(stores.toasts[0].logId);
	assert.match(text, /^page: http:\/\/127\.0\.0\.1:8585\/performance$/m);
	assert.equal(/deck=2/.test(text), false, 'a query can carry ids a report has no business republishing');
});

test('copying a toast that is gone says so rather than copying nothing', async () => {
	const stores = await loadStores();
	await assert.rejects(() => stores.copyToast('t-nope-1'), /no toast with id t-nope-1/);
});

test('an unavailable clipboard throws, and names the insecure-origin fix', async () => {
	install({ clipboard: 'absent', secure: false });
	const stores = await loadStores();
	stores.pushToast('cannot copy me', 'error', 5000);
	await sleep(30);
	await assert.rejects(
		() => stores.copyToast(stores.toasts[0].logId),
		(err) => {
			assert.equal(err.name, 'ClipboardUnavailableError');
			assert.match(err.message, /localhost or 127\.0\.0\.1/);
			return true;
		}
	);
	assert.deepEqual(clipboardWrites, [], 'nothing may reach the clipboard on the failure path');
});

test('a clipboard that refuses the write surfaces the refusal', async () => {
	install({ clipboard: 'reject' });
	const stores = await loadStores();
	stores.pushToast('refused', 'error', 5000);
	await sleep(30);
	await assert.rejects(
		() => stores.copyToast(stores.toasts[0].logId),
		/no user gesture/
	);
});

//-----------------------------------------------------------------------------
// 4. the copied id IS the logged id
//-----------------------------------------------------------------------------

test('the id on screen is the id in the console line somebody greps', async () => {
	const stores = await loadStores();
	stores.pushToast('Deck 2 could not be set as master', 'error', 5000);
	const { logId } = stores.toasts[0];

	const line = consoleLines.find((l) => l.includes('[perf-event] toast-error'));
	assert.ok(line !== undefined, 'every toast writes a ring row');
	assert.match(
		line,
		new RegExp(`id=${logId}(\\s|:)`),
		'the console line is where the id is actually searched for; a structured field ' +
			'nothing prints only helps whoever already knew to open localStorage'
	);
	assert.match(line, /Deck 2 could not be set as master/);
});

test('the id on screen is the id in the durable ring, with the same timestamp', async () => {
	const stores = await loadStores();
	stores.pushToast('ring correlation', 'error', 5000);
	const toast = stores.toasts[0];

	// The ring flush is debounced (FLUSH_DEBOUNCE_MS = 250).
	let rows = [];
	for (let attempt = 0; attempt < 60; attempt += 1) {
		const raw = store.getItem(PERF_KEY);
		if (raw !== null) {
			rows = JSON.parse(raw).filter((r) => r.id === toast.logId);
			if (rows.length > 0) break;
		}
		await sleep(25);
	}

	assert.equal(rows.length, 1, 'exactly one durable row carries this id');
	assert.equal(rows[0].message, 'ring correlation');
	assert.equal(
		rows[0].t,
		toast.createdAt,
		'the copied timestamp must BE the row timestamp, not a second reading of the clock'
	);
});

test('the id on screen is the toast_id the server report carries', async () => {
	const stores = await loadStores();
	stores.pushToast('server correlation', 'error', 5000);
	const { logId } = stores.toasts[0];

	for (let attempt = 0; attempt < 100 && reportedBodies.length === 0; attempt += 1) {
		await sleep(5);
	}
	assert.equal(reportedBodies.length, 1);
	assert.equal(
		reportedBodies[0].context.toast_id,
		logId,
		'a report that cannot be tied to what the user saw is why this id exists'
	);
});

test('the copied id and the logged id are the same string end to end', async () => {
	const stores = await loadStores();
	stores.pushToast('end to end', 'error', 5000);
	const toast = stores.toasts[0];
	await sleep(30);

	const copied = await stores.copyToast(toast.logId);
	const idLine = copied.split('\n').find((l) => l.startsWith('id: '));
	const copiedId = idLine.slice('id: '.length);

	const consoleLine = consoleLines.find((l) => l.includes('[perf-event] toast-error'));
	assert.ok(
		consoleLine.includes(`id=${copiedId}`),
		'this is the single assertion the whole feature rests on: paste the copied id ' +
			'into a log search and the line is there'
	);

	for (let attempt = 0; attempt < 100 && reportedBodies.length === 0; attempt += 1) {
		await sleep(5);
	}
	assert.equal(reportedBodies[0].context.toast_id, copiedId);
});

test('an info toast is correlatable too, though it never reaches the server', async () => {
	const stores = await loadStores();
	stores.pushToast('just so you know', 'info', 5000);
	const { logId } = stores.toasts[0];

	const line = consoleLines.find((l) => l.includes('[perf-event] toast-info'));
	assert.match(
		line,
		new RegExp(`id=${logId}(\\s|:)`),
		'an info toast has no server report at all, so the ring row is its ONLY id; ' +
			'dropping it here leaves half the toasts uncorrelatable'
	);
});

test('two raisings of the identical message get different ids', async () => {
	const stores = await loadStores();
	stores.pushToast('same words', 'error', 5000);
	stores.pushToast('same words', 'error', 5000);

	const [a, b] = stores.toasts;
	assert.notEqual(
		a.logId,
		b.logId,
		'the server report DEDUPES identical messages for 10s, so if the ids matched too ' +
			'there would be no way to tell the two raisings apart anywhere'
	);
	const lines = consoleLines.filter((l) => l.includes('[perf-event] toast-error'));
	assert.equal(lines.length, 2, 'the ring is not deduped, which is why it is the id surface');
	assert.ok(lines[0].includes(`id=${a.logId}`) && lines[1].includes(`id=${b.logId}`));
});

test('the id is minted before either log write, not at render or copy time', () => {
	const source = read('src/lib/stores.svelte.ts');
	const mintAt = source.indexOf('const logId = formatToastId(');
	const ringAt = source.indexOf('const row = recordPerfEvent(');
	const reportAt = source.indexOf('toast_id: logId');
	assert.ok(mintAt !== -1 && ringAt !== -1 && reportAt !== -1);
	assert.ok(
		mintAt < ringAt && ringAt < reportAt,
		'if the id is minted after a log write then that log carries a different id, ' +
			'which is precisely the display-only id this feature exists to avoid'
	);
	assert.equal(
		/formatToastId\(/g.test(source) && (source.match(/formatToastId\(/g) || []).length,
		1,
		'exactly one mint site; a second one is how the screen and the log drift apart'
	);
});

// pin 9bf12adccb45: the toast kind now maps STRAIGHT onto recordPerfEvent's
// info/warn/error severity instead of being flattened into two values, so a
// folded beat-sync lock is a `warn` row rather than an `error` row claiming a
// successful lock failed, or an `info` row hiding it among neutral notes.
//
// - if 'warn' logs as toast-error then a lock that worked reads as a failure
//   wherever the ring is grepped -> broken.
// - if 'warn' logs at info severity then console.warn never fires and the row
//   is indistinguishable from a neutral note -> broken.
test('a warn toast records at warn severity, not flattened to info or error', async () => {
	const stores = await loadStores();
	stores.pushToast('BAR sync locked with a tempo fold', 'warn', 5000);
	const { logId } = stores.toasts[0];

	const index = consoleLines.findIndex((l) => l.includes('[perf-event] toast-warn'));
	assert.ok(
		index !== -1,
		`expected a toast-warn ring row, saw: ${consoleLines.join(' | ') || '(nothing)'}`
	);
	assert.match(consoleLines[index], new RegExp(`id=${logId}(\\s|:)`));
	assert.equal(
		consoleLevels[index],
		'warn',
		'the row must be emitted through console.warn, which is what makes it visible ' +
			'as a warning in a devtools filter rather than as another info line'
	);
	assert.equal(
		consoleLines.filter((l) => l.includes('[perf-event] toast-error')).length,
		0,
		'a fold that locked must never be logged as an error'
	);
});
