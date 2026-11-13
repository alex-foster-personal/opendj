// requirement: KEYB-14215
/**
 * Issue #12286: the /performance hotkey listener across route remounts.
 *
 * The route's onMount calls installPerformanceHotkeys() and its cleanup calls
 * the teardown it returned. This drives the REAL install function the same
 * way, against a fake window that keeps its listener table the way the DOM
 * does (one entry per listener and capture flag), and counts the commands one
 * Space press sends through the UI command boundary. Only that boundary is
 * swapped for a recorder (fixtures/performance-hotkeys-remount-stub-ipc.ts);
 * the routing, keymap runtime and deck-slots state are the real modules.
 *
 * A pending load-play on deck 1 is what Space toggles here, so no audio engine
 * or loaded track is needed: each counted dispatch is one `load_play_intent`.
 *
 * Regression lines:
 * - if two mounts through navigation leave two listeners then one Space sends two transport commands
 * - if a torn-down mount still listens then a press after unmount sends a command
 * - if a remount after teardown does not listen then the shortcut is lost
 * - if a second install with no teardown between stacks a listener then one Space double-dispatches
 */
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { after, afterEach, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const STUB_IPC = fileURLToPath(
	new URL('./fixtures/performance-hotkeys-remount-stub-ipc.ts', import.meta.url)
);

class FakeElement {}

/** A window whose listener table follows the DOM's add/remove matching rules. */
function fakeWindow() {
	const listeners = [];
	const captureOf = (options) =>
		typeof options === 'boolean' ? options : options?.capture === true;
	return {
		listeners,
		addEventListener(type, fn, options) {
			const capture = captureOf(options);
			if (listeners.some((l) => l.type === type && l.fn === fn && l.capture === capture)) return;
			listeners.push({ type, fn, capture });
		},
		removeEventListener(type, fn, options) {
			const capture = captureOf(options);
			const index = listeners.findIndex(
				(l) => l.type === type && l.fn === fn && l.capture === capture
			);
			if (index >= 0) listeners.splice(index, 1);
		},
		dispatch(type, event) {
			for (const l of listeners.filter((entry) => entry.type === type)) l.fn(event);
		}
	};
}

function spaceKeydown() {
	return {
		type: 'keydown',
		code: 'Space',
		key: ' ',
		target: null,
		metaKey: false,
		ctrlKey: false,
		altKey: false,
		shiftKey: false,
		repeat: false,
		timeStamp: 1,
		defaultPrevented: false,
		preventDefault() {
			this.defaultPrevented = true;
		},
		stopPropagation() {}
	};
}

let mod;
let win;
let pending;
const saved = {};

/** Press Space once on the fake window and return the commands that press sent. */
async function pressSpace() {
	mod.sent.length = 0;
	win.dispatch('keydown', spaceKeydown());
	await Promise.resolve();
	return mod.sent.slice();
}

function keydownListenerCount() {
	return win.listeners.filter((l) => l.type === 'keydown').length;
}

before(async () => {
	for (const name of ['window', 'Element']) saved[name] = globalThis[name];
	// Loaded with no window, as the other node tests load it: prefs then reads
	// no storage. The fake window only exists for the listener table.
	mod = await loadTypeScriptModule('tests/unit/fixtures/performance-hotkeys-remount-entry.ts', {
		alias: { '$lib/rb/performance-ipc.svelte': STUB_IPC }
	});
	win = fakeWindow();
	globalThis.window = win;
	globalThis.Element = FakeElement;
});

after(() => {
	for (const [name, value] of Object.entries(saved)) {
		if (value === undefined) delete globalThis[name];
		else globalThis[name] = value;
	}
});

beforeEach(() => {
	win.listeners.length = 0;
	pending = mod.beginPendingLoadPlay(1, false);
});

afterEach(() => {
	mod.clearPendingLoadPlay(1, pending.generation);
});

test('control: one mount turns one Space into one load_play_intent for deck 1', async () => {
	/** [if] the route is mounted once [then] Space sends one transport command for deck 1, [else stop]. */
	const uninstall = mod.installPerformanceHotkeys();
	const commands = await pressSpace();
	uninstall();
	assert.equal(commands.length, 1);
	assert.equal(commands[0].type, 'load_play_intent');
	assert.equal(commands[0].deck, 1);
});

test('two mounts through navigation: one Space press sends one transport command', async () => {
	/** [if] /performance mounts, unmounts and mounts again [then] one Space press sends exactly one command, [else stop]. */
	const firstMount = mod.installPerformanceHotkeys();
	firstMount();
	const secondMount = mod.installPerformanceHotkeys();
	const commands = await pressSpace();
	secondMount();
	assert.equal(commands.length, 1, `one press sent ${commands.length} commands`);
	assert.equal(keydownListenerCount(), 0);
});

test('after unmount the former listener cannot dispatch a command', async () => {
	/** [if] the route unmounts [then] no keydown listener is left and Space sends nothing, [else stop]. */
	const uninstall = mod.installPerformanceHotkeys();
	assert.equal(keydownListenerCount(), 1);
	uninstall();
	assert.equal(keydownListenerCount(), 0);
	assert.deepEqual(await pressSpace(), []);
});

test('after remount the shortcut works once per press, every press', async () => {
	/** [if] the route mounts again after teardown [then] each of three Space presses sends one command, [else stop]. */
	mod.installPerformanceHotkeys()();
	const uninstall = mod.installPerformanceHotkeys();
	const counts = [];
	for (let i = 0; i < 3; i += 1) counts.push((await pressSpace()).length);
	uninstall();
	assert.deepEqual(counts, [1, 1, 1]);
});

test('a second install with no teardown between does not double-dispatch', async () => {
	/** [if] install runs twice with no teardown between [then] one Space still sends one command, [else stop]. */
	const first = mod.installPerformanceHotkeys();
	const second = mod.installPerformanceHotkeys();
	const commands = await pressSpace();
	assert.equal(commands.length, 1, `one press sent ${commands.length} commands`);
	assert.equal(keydownListenerCount(), 1);
	first();
	assert.equal(
		(await pressSpace()).length,
		1,
		"the replaced mount's teardown removed the live listener"
	);
	second();
	assert.deepEqual(await pressSpace(), []);
});
