/**
 * @pytest.mark.requirement A11Y-01
 * Issue #12275: a hotkey-triggered performance command that the dispatcher
 * rejects reports exactly like the pointer press of the same control. The key
 * goes through the REAL installPerformanceHotkeys capture listener into the
 * REAL dispatcher (fixtures/performance-hotkey-rejection-entry.ts, nothing
 * stubbed); a rescue restore in progress is the deterministic rejection, since
 * it refuses `cue` before any engine work.
 *
 * Regression lines:
 * - if a rejected C press leaves performanceCommandStatus.last_error empty then the failure is silent
 * - if a rejected C press adds more than one error toast occurrence then the UI boundary re-reported it
 * - if a rejected C press on deck 2 sets a deck error on another deck then the banner lands on the wrong deck
 * - if the C press and the pointer CUE press of the same rejection report differently then keyboard parity broke
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

class FakeElement {}
class FakeHTMLElement extends FakeElement {}

const TARGET_DECK = 2;
const OTHER_LOADED_DECK = 3;
const REJECTION = /rescue restore owns controls at restoring; command cue rejected/;

const saved = {};
const listeners = new Map();
let m;
let uninstallHotkeys;
let uninstallIpc;

/** Inert performance chrome: not a text field, not a native control, outside the library. */
function chrome() {
	const el = new FakeHTMLElement();
	el.tagName = 'DIV';
	el.isContentEditable = false;
	el.type = '';
	el.getAttribute = () => null;
	el.hasAttribute = () => false;
	el.closest = () => null;
	return el;
}

/** Dispatches one plain letter keydown through the installed capture listener. */
function pressKey(key) {
	const event = {
		type: 'keydown',
		code: `Key${key.toUpperCase()}`,
		key,
		target: chrome(),
		metaKey: false,
		ctrlKey: false,
		altKey: false,
		shiftKey: false,
		repeat: false,
		timeStamp: 1000,
		defaultPrevented: false,
		preventDefault() {
			this.defaultPrevented = true;
		},
		stopPropagation() {}
	};
	const onKey = listeners.get('keydown');
	assert.ok(onKey !== undefined, 'installPerformanceHotkeys must register a keydown listener');
	onKey(event);
	return event;
}

/** Lets the void-ed dispatch promise chain settle (scheduler hops plus the UI catch). */
async function settle() {
	for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

/** Total toast occurrences (grouped toasts count each repeat) whose message names the rejection. */
function rejectionToastOccurrences() {
	return m.toasts
		.filter((t) => t.kind === 'error' && REJECTION.test(t.message))
		.reduce((sum, t) => sum + t.count, 0);
}

/** Runs `body` with console.error captured, returning the captured rows. */
async function withConsoleError(body) {
	const original = console.error;
	const rows = [];
	console.error = (...args) => rows.push(args);
	try {
		await body();
	} finally {
		console.error = original;
	}
	return rows;
}

/** Status snapshot that the keyboard and pointer paths must agree on. */
function reportSnapshot() {
	return {
		last_error: m.performanceCommandStatus.last_error,
		deck_errors: { ...m.performanceCommandStatus.deck_errors },
		toastOccurrences: rejectionToastOccurrences()
	};
}

before(async () => {
	for (const key of ['window', 'Element', 'HTMLElement', 'fetch']) saved[key] = globalThis[key];
	// The build-flag store fetches /api/v1/flags at first read; offline, it fails at once.
	globalThis.fetch = () => Promise.reject(new TypeError('fetch failed: offline unit test'));
	globalThis.Element = FakeElement;
	globalThis.HTMLElement = FakeHTMLElement;
	let recording = false;
	globalThis.window = {
		addEventListener: (type, fn) => {
			if (recording) listeners.set(type, fn);
		},
		removeEventListener: (type) => {
			if (recording) listeners.delete(type);
		},
		// prefs.svelte reads saved settings at import; an empty store is a first boot.
		localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} }
	};
	m = await loadTypeScriptModule('tests/unit/fixtures/performance-hotkey-rejection-entry.ts', {
		viteApiBase: 'https://hotkey-rejection.example.test'
	});
	uninstallIpc = m.installPerformanceBrowserIpc();
	recording = true;
	uninstallHotkeys = m.installPerformanceHotkeys();
	recording = false;
});

after(() => {
	uninstallHotkeys?.();
	m?.setRescueRestorePhaseForTest('idle');
	uninstallIpc?.();
	for (const [key, value] of Object.entries(saved)) {
		if (value === undefined) delete globalThis[key];
		else globalThis[key] = value;
	}
});

beforeEach(() => {
	m.getDeckState(TARGET_DECK).stable_id = 'hotkey-rejection-target';
	m.getDeckState(OTHER_LOADED_DECK).stable_id = 'hotkey-rejection-bystander';
	m.noteRecentDeck(TARGET_DECK);
	m.setRescueRestorePhaseForTest('restoring');
	m.performanceCommandStatus.last_error = null;
	for (const deck of [1, 2, 3, 4]) m.performanceCommandStatus.deck_errors[deck] = null;
	m.toasts.length = 0;
});

test('[if] the C hotkey dispatch is rejected [then] performance command status records its message, [else stop].', async () => {
	await withConsoleError(async () => {
		const event = pressKey('c');
		assert.equal(event.defaultPrevented, true, 'C on chrome is a claimed shortcut');
		await settle();
	});
	assert.match(String(m.performanceCommandStatus.last_error), REJECTION);
});

test('[if] the dispatcher already toasted the C hotkey rejection [then] exactly one error toast occurrence is added, [else stop].', async () => {
	const errorRows = await withConsoleError(async () => {
		pressKey('c');
		await settle();
	});
	assert.equal(
		rejectionToastOccurrences(),
		1,
		'the UI boundary must not re-report a toasted error'
	);
	assert.equal(m.toasts.length, 1, JSON.stringify(m.toasts.map((t) => t.message)));
	// Only rows about this rejection: the offline build-flag fetch logs its own unrelated row.
	const rejectionRows = errorRows.filter((args) => args.some((a) => REJECTION.test(String(a))));
	assert.equal(rejectionRows.length, 1, 'one console.error row, from the one toast');
});

test('[if] the rejected C hotkey names deck 2 [then] only deck 2 receives the deck error, [else stop].', async () => {
	await withConsoleError(async () => {
		pressKey('c');
		await settle();
	});
	const { deck_errors } = m.performanceCommandStatus;
	assert.match(String(deck_errors[TARGET_DECK]), REJECTION);
	for (const deck of [1, 3, 4]) {
		assert.equal(
			deck_errors[deck],
			null,
			`deck ${deck} must not carry deck ${TARGET_DECK}'s error`
		);
	}
});

test('[if] the C hotkey and the pointer CUE press hit the same rejection [then] both leave the same status and toast count, [else stop].', async () => {
	await withConsoleError(async () => {
		pressKey('c');
		await settle();
	});
	const keyboard = reportSnapshot();

	m.performanceCommandStatus.last_error = null;
	for (const deck of [1, 2, 3, 4]) m.performanceCommandStatus.deck_errors[deck] = null;
	m.toasts.length = 0;
	let result;
	await withConsoleError(async () => {
		result = await m.runPerformanceCommandFromUi({ type: 'cue', deck: TARGET_DECK });
	});
	assert.equal(result.ok, false);
	assert.equal(result.reason, 'failed');
	assert.deepEqual(reportSnapshot(), keyboard);
});
