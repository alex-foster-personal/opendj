/**
 * #12272: the hotkeys overlay through the performance IPC. An agent reads
 * whether it is open from the performance state and the UI mirror, and opens
 * or closes it with the typed `hotkeys_overlay` command, on the same overlay
 * state machine the "?" key toggles, without touching transport.
 *
 * Regression lines:
 *   - if queryPerformanceState or the UI mirror does not report the "?" overlay as open then broken
 *   - if an agent open does not open the store "?" toggles then broken
 *   - if an agent close sends anything to the engine or moves a deck then broken
 *   - if the command parses without a boolean open or gains a queue scope then broken
 */
import assert from 'node:assert/strict';
import { after, afterEach, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let m;
let engineSends;
let uninstallIpc;

before(async () => {
	m = await loadTypeScriptModule('tests/unit/fixtures/hotkeys-overlay-command-entry.ts');
});

beforeEach(() => {
	engineSends = [];
	m.installRustEngineClientForTest({
		connected: true,
		notBuilt: new Set(),
		send: (cmd) => {
			engineSends.push(cmd);
			return Promise.resolve({ type: 'result', id: null, ok: true });
		},
		positionMs: () => null
	});
	globalThis.window = {};
	uninstallIpc = m.ipc.installPerformanceBrowserIpc();
	m.initRustMode();
	m.rustMode.enabled = true;
	m.overlay.hideHotkeysOverlay();
});

afterEach(() => {
	m.overlay.hideHotkeysOverlay();
	uninstallIpc();
	delete globalThis.window;
});

after(() => {
	m.installRustEngineClientForTest(null);
	m.rustMode.enabled = false;
});

/** What an agent sends: the window IPC bridge, as `opendj` and MCP orders reach the page. */
function agentDispatch(command) {
	return globalThis.window.musicDjToolsPerformance.dispatch(command);
}

/** The UI mirror as the page publishes it, with an empty document for its controls list. */
function publishedMirror() {
	globalThis.document = { querySelectorAll: () => [], visibilityState: 'visible' };
	try {
		return m.mirror.buildUiMirror();
	} finally {
		delete globalThis.document;
	}
}

/** Each deck's transport as an agent reads it. */
function transport() {
	const decks = m.ipc.queryPerformanceState().decks;
	return [1, 2, 3, 4].map((deck) => ({
		playing: decks[deck].playing,
		position_ms: decks[deck].position_ms,
		stable_id: decks[deck].stable_id
	}));
}

test('an agent querying performance state sees whether the hotkeys overlay is open', () => {
	/** [if] "?" opens then closes the overlay [then] query and the UI mirror report open then closed, [else stop]. */
	assert.equal(m.ipc.queryPerformanceState().hotkeys_overlay_open, false);
	assert.equal(publishedMirror().hotkeys_overlay_open, false);
	m.overlay.toggleHotkeysOverlay();
	assert.equal(m.ipc.queryPerformanceState().hotkeys_overlay_open, true);
	assert.equal(globalThis.window.musicDjToolsPerformance.query().hotkeys_overlay_open, true);
	assert.equal(publishedMirror().hotkeys_overlay_open, true);
	m.overlay.toggleHotkeysOverlay();
	assert.equal(m.ipc.queryPerformanceState().hotkeys_overlay_open, false);
});

test('an agent open opens the same overlay state the ? key toggles', async () => {
	/** [if] an agent sends hotkeys_overlay open true [then] the "?" store reads open and one "?" closes it, [else stop]. */
	await agentDispatch({ type: 'hotkeys_overlay', open: true });
	assert.equal(m.overlay.isHotkeysOverlayOpen(), true);
	assert.equal(m.overlay.hotkeysOverlay.open, true);
	assert.equal(m.ipc.queryPerformanceState().hotkeys_overlay_open, true);
	// Opening again is idempotent: the overlay stays open rather than toggling shut.
	await agentDispatch({ type: 'hotkeys_overlay', open: true });
	assert.equal(m.overlay.isHotkeysOverlayOpen(), true);
	m.overlay.toggleHotkeysOverlay();
	assert.equal(m.overlay.isHotkeysOverlayOpen(), false);
	assert.equal(m.ipc.queryPerformanceState().hotkeys_overlay_open, false);
});

test('an agent close shuts the overlay and leaves transport untouched', async () => {
	/** [if] an agent closes the overlay while deck 1 plays [then] it closes, no engine send, decks unchanged, [else stop]. */
	m.deckStates[1].playing = true;
	try {
		m.overlay.toggleHotkeysOverlay();
		assert.equal(m.overlay.isHotkeysOverlayOpen(), true);
		const before = transport();
		assert.equal(before[0].playing, true);
		await agentDispatch({ type: 'hotkeys_overlay', open: false });
		assert.equal(m.overlay.isHotkeysOverlayOpen(), false);
		assert.equal(m.ipc.queryPerformanceState().hotkeys_overlay_open, false);
		assert.deepEqual(engineSends, []);
		assert.deepEqual(transport(), before);
		assert.equal(m.ipc.queryPerformanceState().last_error, null);
	} finally {
		m.deckStates[1].playing = false;
	}
});

test('the hotkeys_overlay command is typed and runs outside every deck queue', async () => {
	/** [if] hotkeys_overlay parses without a boolean open or with extra keys [then] the wire is untyped, [else stop]. */
	assert.deepEqual(m.guards._parseCommand({ type: 'hotkeys_overlay', open: false }), {
		type: 'hotkeys_overlay',
		open: false
	});
	assert.throws(() => m.guards._parseCommand({ type: 'hotkeys_overlay' }), /open must be boolean/);
	assert.throws(() => m.guards._parseCommand({ type: 'hotkeys_overlay', open: 'yes' }), /boolean/);
	assert.throws(() => m.guards._parseCommand({ type: 'hotkeys_overlay', open: true, deck: 1 }));
	assert.equal(m.ipc.performanceCommandQueueScopes({ type: 'hotkeys_overlay', open: true }), null);
	await assert.rejects(agentDispatch({ type: 'hotkeys_overlay', open: 1 }), /boolean/);
	assert.equal(m.overlay.isHotkeysOverlayOpen(), false);
});
