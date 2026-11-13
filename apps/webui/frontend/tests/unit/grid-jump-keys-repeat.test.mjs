/**
 * Issue #12276 (A11Y-01): a held Cmd/Ctrl grid-jump chord on /performance.
 *
 * This drives the REAL installPerformanceHotkeys() listener on a fake window,
 * with the `deck.snapped_jump` flag declared ON through the real build-flag
 * store (its fetch answers /api/v1/flags), so the flag is read the way the app
 * reads it rather than passed in as routing context. Only the UI command
 * boundary is swapped for a recorder (fixtures/grid-jump-keys-repeat-stub-ipc.ts);
 * the routing, keymap runtime, recent-deck and deck state are the real modules.
 * Deck 1 carries a stable id so the transport deck resolves to it.
 *
 * Regression lines:
 * - if a held chord sends one grid_jump per keydown repeat then broken
 * - if a repeat keydown is not default-prevented then Select all or a bookmark runs mid-hold
 * - if a text field's own Cmd/Ctrl line keys send a grid_jump then broken
 */
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { after, afterEach, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const STUB_IPC = fileURLToPath(
	new URL('./fixtures/grid-jump-keys-repeat-stub-ipc.ts', import.meta.url)
);
const API_BASE = 'https://grid-jump-repeat.example.test';
const FLAGS_PATH = '/api/v1/flags';
const GRID_JUMP_FLAG = {
	flag_id: 'deck.snapped_jump',
	enabled: true,
	default: false,
	overridden: true,
	note: '',
	owner: 'deck'
};
const REPEATS = 3;

/** Stand-in for DOM elements: `closest` answers only the selector it was built for. */
class FakeElement {
	constructor(selector) {
		this.selector = selector;
	}
	closest(selector) {
		return selector === this.selector ? this : null;
	}
}

const PAGE = { tagName: 'DIV', getAttribute: () => null };
const TEXT_FIELD = { tagName: 'INPUT', type: 'text', getAttribute: () => null };
const DECK_SURFACE = new FakeElement('[data-deck-hover]');

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

/** A keydown that remembers whether the listener prevented or stopped it. */
function chordKeydown(
	key,
	{ modifier = 'metaKey', repeat = false, target = PAGE, shift = false } = {}
) {
	return {
		type: 'keydown',
		code: '',
		key,
		target,
		metaKey: modifier === 'metaKey',
		ctrlKey: modifier === 'ctrlKey',
		altKey: false,
		shiftKey: shift,
		repeat,
		timeStamp: 1,
		defaultPrevented: false,
		propagationStopped: false,
		preventDefault() {
			this.defaultPrevented = true;
		},
		stopPropagation() {
			this.propagationStopped = true;
		}
	};
}

let mod;
let win;
let uninstall;
const saved = {};

/** Hold a chord: the initial keydown plus REPEATS auto-repeat keydowns; returns every event. */
async function holdChord(key, options = {}) {
	const events = [chordKeydown(key, options)];
	for (let i = 0; i < REPEATS; i += 1) events.push(chordKeydown(key, { ...options, repeat: true }));
	for (const event of events) win.dispatch('keydown', event);
	await Promise.resolve();
	return events;
}

function gridJumps() {
	return mod.recorded.filter((command) => command.type === 'grid_jump');
}

/** The last pointer press lands on a deck, which is what lets Cmd+A mean "jump back". */
function pressDeckSurface() {
	win.dispatch('pointerdown', { type: 'pointerdown', target: DECK_SURFACE });
}

before(async () => {
	for (const name of ['window', 'Element', 'fetch']) saved[name] = globalThis[name];
	// Loaded with no window, as the other node tests load it: prefs then reads
	// no storage. The fake window only exists for the listener table.
	mod = await loadTypeScriptModule('tests/unit/fixtures/grid-jump-keys-repeat-entry.ts', {
		viteApiBase: API_BASE,
		alias: { '$lib/rb/performance-ipc.svelte': STUB_IPC }
	});
	globalThis.fetch = async (input) => {
		const url = input instanceof Request ? input.url : String(input);
		const { pathname } = new URL(url, API_BASE);
		if (pathname !== FLAGS_PATH) throw new Error(`unexpected request ${pathname}`);
		return Response.json({ flags: [GRID_JUMP_FLAG], build_profile: 'full', sandboxed: false });
	};
	await mod.buildFlags.load();
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
	mod.recorded.length = 0;
	mod._resetRecentDeckForTests();
	mod.deckStates[1].stable_id = 'grid-jump-repeat-track';
	uninstall = mod.installPerformanceHotkeys();
});

afterEach(() => {
	uninstall();
	mod.deckStates[1].stable_id = null;
});

test('control: the flag is on through the store and one Cmd+Right sends one grid_jump', async () => {
	/** [if] the daemon declares deck.snapped_jump on [then] one Cmd+Right press sends grid_jump 4 bars on deck 1, [else stop]. */
	assert.equal(mod.buildFlags.loaded, true);
	assert.equal(mod.buildFlags.flag('deck.snapped_jump')?.enabled, true);
	win.dispatch('keydown', chordKeydown('ArrowRight'));
	await Promise.resolve();
	assert.deepEqual(gridJumps(), [{ type: 'grid_jump', deck: 1, bars: 4 }]);
});

test('a held Cmd or Ctrl grid-jump chord dispatches grid_jump only on the initial keydown', async () => {
	/** [if] Cmd or Ctrl plus a grid-jump key is held through repeats [then] exactly one grid_jump is sent, [else stop]. */
	const cases = [
		['metaKey', 'ArrowRight', {}, 4],
		['ctrlKey', 'ArrowRight', {}, 4],
		['metaKey', 'ArrowLeft', {}, -4],
		['ctrlKey', 'ArrowLeft', { shift: true }, -8],
		['metaKey', 'd', {}, 4],
		['ctrlKey', 'd', {}, 4]
	];
	for (const [modifier, key, extra, bars] of cases) {
		mod.recorded.length = 0;
		await holdChord(key, { modifier, ...extra });
		assert.deepEqual(
			gridJumps(),
			[{ type: 'grid_jump', deck: 1, bars }],
			`${modifier}+${key} held sent ${gridJumps().length} grid_jump commands`
		);
	}
});

test('every repeat keydown of a held chord is default-prevented so Select all and bookmark never run', async () => {
	/** [if] repeat keydowns of Cmd/Ctrl+A or +D arrive [then] each one is prevented and stopped, [else stop]. */
	pressDeckSurface();
	for (const modifier of ['metaKey', 'ctrlKey']) {
		for (const key of ['a', 'd']) {
			mod.recorded.length = 0;
			const events = await holdChord(key, { modifier });
			assert.equal(gridJumps().length, 1, `${modifier}+${key} held should jump once`);
			for (const [index, event] of events.entries()) {
				assert.equal(
					event.defaultPrevented,
					true,
					`${modifier}+${key} keydown ${index} fell through`
				);
				assert.equal(
					event.propagationStopped,
					true,
					`${modifier}+${key} keydown ${index} propagated`
				);
			}
		}
	}
});

test('with focus in a text field the grid-jump chord does not dispatch and keeps its default', async () => {
	/** [if] focus is in a text field and a Cmd/Ctrl grid-jump chord is held [then] no grid_jump is sent, [else stop]. */
	pressDeckSurface();
	for (const modifier of ['metaKey', 'ctrlKey']) {
		for (const key of ['ArrowRight', 'ArrowLeft', 'a', 'd']) {
			const events = await holdChord(key, { modifier, target: TEXT_FIELD });
			assert.deepEqual(gridJumps(), [], `${modifier}+${key} in a text field dispatched`);
			assert.ok(
				events.every((event) => !event.defaultPrevented),
				`${modifier}+${key} in a text field lost its own default`
			);
		}
	}
});
