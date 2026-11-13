/**
 * @pytest.mark.requirement A11Y-01
 * @pytest.mark.requirement A11Y-14348
 * Issue #12289: a hotkey pressed while no performance command session exists
 * reports a truthful `no_session` result, leaves nothing pending, and does not
 * stop the next hotkey from dispatching once a session is installed again. The
 * key goes through the REAL installPerformanceHotkeys capture listener into the
 * REAL dispatcher (fixtures/performance-hotkey-session-recovery-entry.ts); only
 * fetch is stubbed. A pending load-play gives Space a command that succeeds
 * without an audio engine (load_play_intent edits the pending intent).
 *
 * Regression lines:
 * - if a C press with no session throws, toasts or sets last_error then the unavailable result is not truthful
 * - if the UI boundary returns anything but reason no_session with no session then the IPC layer lies about it
 * - if Space after a session reinstall leaves the pending intent unchanged then a failed session disabled hotkeys
 * - if a failed Cmd+Space flips or quantizes the pending intent then a failed press left a pending action behind
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

class FakeElement {}
class FakeHTMLElement extends FakeElement {}

const TARGET_DECK = 2;
const NO_SESSION = /not run, no command session/;

const saved = {};
const listeners = new Map();
let m;
let uninstallHotkeys;
let uninstallIpc = null;
let unhandled = [];
const onUnhandled = (reason) => unhandled.push(reason);

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

/** Dispatches one keydown through the installed capture listener. */
function press({ code, key, metaKey = false }) {
	const event = {
		type: 'keydown',
		code,
		key,
		target: chrome(),
		metaKey,
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

const C = { code: 'KeyC', key: 'c' };
const SPACE = { code: 'Space', key: ' ' };
const CMD_SPACE = { code: 'Space', key: ' ', metaKey: true };

/** Lets the void-ed dispatch promise chain settle (scheduler hops plus the UI catch). */
async function settle() {
	for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

/** Runs `body` with console.debug and console.error captured, returning the rows. */
async function withConsole(body) {
	const original = { debug: console.debug, error: console.error };
	const rows = { debug: [], error: [] };
	console.debug = (...args) => rows.debug.push(args.map(String).join(' '));
	console.error = (...args) => rows.error.push(args.map(String).join(' '));
	try {
		await body();
	} finally {
		console.debug = original.debug;
		console.error = original.error;
	}
	return rows;
}

/** Nothing queued, running or pending on any deck in the dispatcher's status. */
function assertNothingPending() {
	const s = m.performanceCommandStatus;
	assert.equal(s.active, 0, 'no command may stay active');
	assert.equal(s.queued, 0, 'no command may stay queued');
	assert.deepEqual({ ...s.deck_pending }, { 1: 0, 2: 0, 3: 0, 4: 0 });
}

function startSession() {
	uninstallIpc = m.installPerformanceBrowserIpc();
}

function endSession() {
	uninstallIpc?.();
	uninstallIpc = null;
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
	m = await loadTypeScriptModule(
		'tests/unit/fixtures/performance-hotkey-session-recovery-entry.ts',
		{
			viteApiBase: 'https://hotkey-session-recovery.example.test'
		}
	);
	m.setRescueRestorePhaseForTest('idle');
	recording = true;
	uninstallHotkeys = m.installPerformanceHotkeys();
	recording = false;
	process.on('unhandledRejection', onUnhandled);
});

after(() => {
	process.off('unhandledRejection', onUnhandled);
	uninstallHotkeys?.();
	endSession();
	for (const [key, value] of Object.entries(saved)) {
		if (value === undefined) delete globalThis[key];
		else globalThis[key] = value;
	}
});

beforeEach(() => {
	endSession();
	unhandled = [];
	m.getDeckState(TARGET_DECK).stable_id = 'session-recovery-target';
	m.noteRecentDeck(TARGET_DECK);
	m.toasts.length = 0;
});

test('[if] C fires before a command session exists [then] it reports no_session without throwing, toasting or erroring, [else stop].', async () => {
	let event;
	const rows = await withConsole(async () => {
		event = press(C);
		await settle();
	});
	assert.equal(event.defaultPrevented, true, 'C on chrome is still a claimed shortcut');
	assert.equal(rows.debug.filter((row) => NO_SESSION.test(row)).length, 1, rows.debug.join('\n'));
	assert.deepEqual(unhandled, [], 'the void-ed hotkey dispatch must not reject unhandled');
	assert.equal(m.performanceCommandStatus.last_error, null, 'no session is not a command failure');
	assert.equal(m.toasts.filter((t) => t.kind === 'error').length, 0);

	let result;
	await withConsole(async () => {
		result = await m.runPerformanceCommandFromUi({ type: 'cue', deck: TARGET_DECK });
	});
	assert.equal(result.ok, false);
	assert.equal(result.reason, 'no_session', 'the UI boundary names the missing session');
});

test('[if] a session ends, a hotkey fails, then the session reinstalls [then] the next Space dispatches normally, [else stop].', async () => {
	startSession();
	endSession();
	const pending = m.beginPendingLoadPlay(TARGET_DECK, false);
	const failed = await withConsole(async () => {
		press(SPACE);
		await settle();
	});
	assert.equal(
		failed.debug.filter((row) => NO_SESSION.test(row)).length,
		1,
		failed.debug.join('\n')
	);
	assert.equal(m.pendingLoadPlayState()[TARGET_DECK].desiredPlay, false, 'no session, no edit');

	startSession();
	const recovered = await withConsole(async () => {
		press(SPACE);
		await settle();
	});
	assert.deepEqual(
		recovered.debug.filter((row) => NO_SESSION.test(row)),
		[]
	);
	// The offline build-flag fetch logs its own unrelated row; every other row is a failure.
	const commandErrors = recovered.error.filter((row) => !row.startsWith('[store-build]'));
	assert.deepEqual(commandErrors, [], 'the recovered Space must not log a failure');
	const intent = m.pendingLoadPlayState()[TARGET_DECK];
	assert.equal(intent.generation, pending.generation);
	assert.equal(intent.desiredPlay, true, 'Space after the reinstall must reach the dispatcher');
	assert.equal(m.performanceCommandStatus.last_error, null);
	assert.equal(m.toasts.filter((t) => t.kind === 'error').length, 0);
	assertNothingPending();
	assert.deepEqual(unhandled, []);
});

test('[if] Cmd+Space fails for want of a session [then] the pending intent is unchanged and nothing stays queued, [else stop].', async () => {
	const pending = m.beginPendingLoadPlay(TARGET_DECK, false);
	await withConsole(async () => {
		press(CMD_SPACE);
		await settle();
	});
	const intent = m.pendingLoadPlayState()[TARGET_DECK];
	assert.equal(intent.generation, pending.generation);
	assert.equal(intent.desiredPlay, false, 'a press that never ran must not arm a play');
	assert.equal(
		intent.quantize,
		undefined,
		'a press that never ran must not arm a quantized launch'
	);
	assertNothingPending();
	assert.deepEqual(unhandled, []);
});
