/**
 * #12280: a keyboard-originated command refusal carries a stable code in the
 * performance command status next to its toast text, the UI mirror an agent
 * reads (CLI `opendj state`, MCP `app_state`) carries the same code, and a
 * later successful command clears it.
 *
 * Regression lines:
 *   - if an engine refusal's own code is not in last_error_code then broken
 *   - if the UI mirror does not publish the same code then broken
 *   - if a success after a refusal leaves the old code then broken
 *   - if an uncoded refusal gets a guessed code instead of unknown then broken
 */
import assert from 'node:assert/strict';
import { after, afterEach, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let m;
let engineAnswer;
let uninstallIpc;

before(async () => {
	m = await loadTypeScriptModule('tests/unit/fixtures/performance-refusal-code-entry.ts');
});

beforeEach(() => {
	engineAnswer = { ok: true };
	m.installRustEngineClientForTest({
		connected: true,
		notBuilt: new Set(),
		send: (cmd) =>
			engineAnswer.ok
				? Promise.resolve({ type: 'result', id: null, ok: true })
				: Promise.reject(
						new m.EngineCommandError(engineAnswer.code, `${cmd.type}: ${engineAnswer.message}`)
					),
		positionMs: () => null
	});
	// The browser IPC opens the command session a keyboard press runs in.
	globalThis.window = {};
	uninstallIpc = m.ipc.installPerformanceBrowserIpc();
	m.initRustMode();
	m.rustMode.enabled = true;
});

afterEach(() => {
	uninstallIpc();
	delete globalThis.window;
});

after(() => {
	m.installRustEngineClientForTest(null);
	m.rustMode.enabled = false;
});

/** The UI mirror as the page publishes it, with an empty document for its controls list. */
function publishedMirror() {
	globalThis.document = { querySelectorAll: () => [], visibilityState: 'visible' };
	try {
		return m.mirror.buildUiMirror();
	} finally {
		delete globalThis.document;
	}
}

test('a hotkey fader move the engine refuses records the engine code next to the toast text', async () => {
	/** [if] the engine refuses a keyboard fader move with `invalid` [then] the status carries code invalid beside its text, [else stop]. */
	engineAnswer = { ok: false, code: 'invalid', message: 'value 7 is out of range' };
	const result = await m.ipc.runPerformanceCommandFromUi({ type: 'fader', deck: 1, value: 0.5 });
	assert.equal(result.ok, false);
	const state = m.ipc.queryPerformanceState();
	assert.match(state.last_error ?? '', /value 7 is out of range/);
	assert.equal(state.last_error_code, 'invalid');
	assert.equal(state.decks[1].command_error_code, 'invalid');
	assert.equal(state.decks[2].command_error_code, null);
});

test('the UI mirror an agent reads reports the same refusal code', async () => {
	/** [if] a hotkey refusal is read back through the UI mirror [then] it carries the same code and text, [else stop]. */
	engineAnswer = { ok: false, code: 'no_beatgrid', message: 'deck 1 has no beatgrid' };
	await m.ipc.runPerformanceCommandFromUi({ type: 'fader', deck: 1, value: 0.5 });
	const state = m.ipc.queryPerformanceState();
	const mirror = publishedMirror();
	assert.equal(mirror.last_error_code, 'no_beatgrid');
	assert.equal(mirror.last_error, state.last_error);
	assert.equal(mirror.decks['1'].command_error_code, 'no_beatgrid');
	assert.equal(mirror.decks['1'].command_error, state.decks[1].command_error);
});

test('a successful command after a refusal clears the stale code', async () => {
	/** [if] a refused hotkey command is followed by one that succeeds [then] last_error_code and the deck code read null, [else stop]. */
	engineAnswer = { ok: false, code: 'invalid', message: 'value 7 is out of range' };
	await m.ipc.runPerformanceCommandFromUi({ type: 'fader', deck: 1, value: 0.5 });
	assert.equal(m.ipc.queryPerformanceState().last_error_code, 'invalid');
	engineAnswer = { ok: true };
	const result = await m.ipc.runPerformanceCommandFromUi({ type: 'fader', deck: 1, value: 0.6 });
	assert.equal(result.ok, true);
	const state = m.ipc.queryPerformanceState();
	assert.equal(state.last_error, null);
	assert.equal(state.last_error_code, null);
	assert.equal(state.decks[1].command_error_code, null);
	assert.equal(publishedMirror().last_error_code, null);
});

test('play on an empty deck from the keyboard maps to the engine code no_track', async () => {
	/** [if] Space plays an empty deck (a page refusal with no code) [then] it reads as the engine's no_track, [else stop]. */
	assert.equal(m.deckStates[3].stable_id, null);
	await m.ipc.runPerformanceCommandFromUi({ type: 'play', deck: 3, playing: true });
	const state = m.ipc.queryPerformanceState();
	assert.match(state.last_error ?? '', /Deck 3 is empty/);
	assert.equal(state.last_error_code, 'no_track');
});

test('an uncoded refusal is the explicit unknown code and keeps its reason text', () => {
	/** [if] a refusal names no code this module knows [then] it maps to unknown and the text is untouched, [else stop]. */
	const book = m.refusal.createRefusalCodeBook(2);
	book.note('Error: the deck lost its decoder', new Error('the deck lost its decoder'));
	assert.equal(book.codeFor('Error: the deck lost its decoder'), 'unknown');
	assert.equal(book.codeFor('a text nothing noted'), 'unknown');
	assert.equal(book.codeFor(null), null);
	assert.equal(m.refusal.refusalCodeOf({ code: 'not a code' }), 'unknown');
	assert.equal(
		m.refusal.refusalCodeOf(new Error('RUST_ENGINE_UNAVAILABLE: deck_move is not available')),
		'not_implemented'
	);
});

test('the code book forgets its oldest text first and refuses a bad capacity', () => {
	/** [if] more refusal texts are noted than the book holds [then] the oldest reads unknown and newer keep codes, [else stop]. */
	const book = m.refusal.createRefusalCodeBook(2);
	book.note('a', new m.EngineCommandError('decode', 'a'));
	book.note('b', new m.EngineCommandError('io', 'b'));
	book.note('c', new m.EngineCommandError('wrong_clock', 'c'));
	assert.equal(book.codeFor('a'), 'unknown');
	assert.equal(book.codeFor('b'), 'io');
	assert.equal(book.codeFor('c'), 'wrong_clock');
	assert.throws(() => m.refusal.createRefusalCodeBook(0), /positive integer/);
});
