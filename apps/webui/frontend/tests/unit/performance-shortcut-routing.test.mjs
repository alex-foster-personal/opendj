/**
 * Registration guards only. Issue5761's behavior is exercised by the real
 * /performance browser in tests/live/performance-focused-controls.mjs.
 * Supersedes the FakeElement/handler stand-ins and fabricated feedback state
 * that could not prove native activation or unchanged real transport.
 *
 * KEYB-11711 regression lines:
 * - if , . fail to resolve to tempo-step then broken
 * - if Shift+K fails to reset tempo then broken
 * - if text fields lose , . typing then broken
 *
 * KEYB-11383 regression lines:
 * - if C, V, K or Backquote fire inside a text field or with Cmd/Ctrl/Alt then broken
 * - if an empty recent deck sends a command then broken
 * - if Settings open still routes the chords then broken
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

test('performance-hotkeys registers keydown in capture phase so body Space cannot scroll', async () => {
	const source = await readFile('src/lib/rb/performance-hotkeys.ts', 'utf8');
	assert.match(source, /addEventListener\('keydown', onKey, \{ capture: true \}\)/);
});

test('performance-hotkeys uses shared routing rather than a second focus check', async () => {
	const source = await readFile('src/lib/rb/performance-hotkeys.ts', 'utf8');
	assert.match(source, /handlePerformanceShortcutKeydown/);
	assert.doesNotMatch(source, /isNativeInteractiveTarget/);
});

let routing;

before(async () => {
	routing = await loadTypeScriptModule('src/lib/rb/performance-shortcut-routing.ts');
});

const PAGE = { tagName: 'DIV', getAttribute: () => null };
const TEXT = { tagName: 'INPUT', type: 'text', getAttribute: () => null };

function bareKey(k, extra = {}) {
	return {
		code: '',
		key: k,
		target: PAGE,
		metaKey: false,
		ctrlKey: false,
		altKey: false,
		shiftKey: false,
		repeat: false,
		...extra
	};
}

const resolve = (e) => routing.resolvePerformanceShortcutAction(e, {});

// ----- KEYB-11383: C cue, V beat sync, K master tempo, Backquote cycles -----

test('C, V, K resolve to the recent-deck verbs (Caps Lock too); Backquote cycles, Shift reverses', () => {
	assert.deepEqual(resolve(bareKey('c')), { kind: 'recent-deck', verb: 'cue', repeat: false });
	assert.deepEqual(resolve(bareKey('C')), { kind: 'recent-deck', verb: 'cue', repeat: false });
	assert.deepEqual(resolve(bareKey('v')), {
		kind: 'recent-deck',
		verb: 'beat-sync',
		repeat: false
	});
	assert.deepEqual(resolve(bareKey('k')), {
		kind: 'recent-deck',
		verb: 'master-tempo',
		repeat: false
	});
	assert.deepEqual(resolve(bareKey('`', { code: 'Backquote' })), {
		kind: 'cycle-deck',
		direction: 1,
		repeat: false
	});
	assert.deepEqual(resolve(bareKey('~', { code: 'Backquote', shiftKey: true })), {
		kind: 'cycle-deck',
		direction: -1,
		repeat: false
	});
});

test('the new chords are ignored in text entry, with Cmd, Ctrl or Alt, and Shift+C', () => {
	const cases = [bareKey('c'), bareKey('v'), bareKey('k'), bareKey('`', { code: 'Backquote' })];
	for (const base of cases) {
		for (const extra of [
			{ target: TEXT },
			{ metaKey: true },
			{ ctrlKey: true },
			{ altKey: true }
		]) {
			assert.equal(
				routing.resolvePerformanceShortcutAction({ ...base, ...extra }),
				null,
				`${base.key} ${JSON.stringify(extra)}`
			);
		}
	}
	assert.equal(routing.resolvePerformanceShortcutAction(bareKey('C', { shiftKey: true })), null);
});

test('Settings open routes nothing; a held key prevents default but fires once only', () => {
	const calls = [];
	const handlers = {
		recentDeckCommand: (v) => calls.push(v),
		cycleRecentDeck: (d) => calls.push(d)
	};
	let prevented = 0;
	const kd = (e) => ({ ...e, timeStamp: 0, preventDefault: () => (prevented += 1) });
	assert.equal(
		routing.handlePerformanceShortcutKeydown(kd(bareKey('c')), handlers, { settingsOpen: true }),
		false
	);
	assert.equal(
		routing.handlePerformanceShortcutKeydown(kd(bareKey('c', { repeat: true })), handlers),
		true
	);
	assert.equal(
		routing.handlePerformanceShortcutKeydown(
			kd(bareKey('`', { code: 'Backquote', repeat: true })),
			handlers
		),
		true
	);
	assert.deepEqual(calls, []);
	assert.equal(prevented, 2);
	routing.handlePerformanceShortcutKeydown(kd(bareKey('k')), handlers);
	routing.handlePerformanceShortcutKeydown(
		kd(bareKey('~', { code: 'Backquote', shiftKey: true })),
		handlers
	);
	assert.deepEqual(calls, ['master-tempo', -1]);
});

// A11Y-01: the focused-button Space rule needs a real DOM Element, so it stays
// pinned by tests/live/performance-focused-controls.mjs; here we pin that the
// Space branch still checks it before anything else.
test('A11Y-01: the Space branch still defers to a focused native control', async () => {
	const source = await readFile('src/lib/rb/performance-shortcut-routing.ts', 'utf8');
	assert.match(source, /if \(isNativeInteractiveTarget\(e\.target\)\) return null;/);
});

test('buildRecentDeckCommand flips toggles from deck state and sends nothing for an empty deck', () => {
	const st = { stable_id: 'abc', beat_sync_enabled: false, master_tempo_enabled: true };
	assert.deepEqual(routing.buildRecentDeckCommand('cue', 2, st), { type: 'cue', deck: 2 });
	assert.deepEqual(routing.buildRecentDeckCommand('beat-sync', 2, st), {
		type: 'beat_sync',
		deck: 2,
		enabled: true
	});
	assert.deepEqual(
		routing.buildRecentDeckCommand('beat-sync', 2, { ...st, beat_sync_enabled: true }),
		{
			type: 'beat_sync',
			deck: 2,
			enabled: false
		}
	);
	assert.deepEqual(routing.buildRecentDeckCommand('master-tempo', 2, st), {
		type: 'master_tempo',
		deck: 2,
		enabled: false
	});
	assert.equal(routing.buildRecentDeckCommand('cue', 2, { ...st, stable_id: null }), null);
	assert.equal(routing.buildRecentDeckCommand('cue', null, null), null);
});

test('performance-hotkeys wires the chords through the shared builders', async () => {
	const source = await readFile('src/lib/rb/performance-hotkeys.ts', 'utf8');
	assert.match(source, /buildRecentDeckCommand\(/);
	assert.match(source, /cycleRecentDeck\(/);
});

test('the ? overlay lists C, V, K and Backquote under Performance', async () => {
	const { HOTKEY_REGISTRY } = await loadTypeScriptModule(
		'src/lib/components/rb/hotkeys/hotkeys-registry.ts'
	);
	const byId = Object.fromEntries(HOTKEY_REGISTRY.map((e) => [e.id, e]));
	for (const [id, chord] of [
		['performance-cue', /^C$/],
		['performance-beat-sync', /^V$/],
		['performance-master-tempo', /^K$/],
		['performance-cycle-deck', /`/]
	]) {
		assert.equal(byId[id]?.group, 'Performance', id);
		assert.match(byId[id].chord, chord);
		assert.ok(byId[id].description.length > 0);
	}
});

// ----- KEYB-11711: tempo fine/coarse, Shift+K reset, P pitch range -----

test('KEYB-11711: , and . resolve to tempo-step fine; shift uses > and <', () => {
	assert.deepEqual(resolve(bareKey('.')), { kind: 'tempo-step', direction: 1, coarse: false });
	assert.deepEqual(resolve(bareKey(',')), { kind: 'tempo-step', direction: -1, coarse: false });
	assert.deepEqual(resolve(bareKey('>', { shiftKey: true })), {
		kind: 'tempo-step',
		direction: 1,
		coarse: true
	});
	assert.deepEqual(resolve(bareKey('<', { shiftKey: true })), {
		kind: 'tempo-step',
		direction: -1,
		coarse: true
	});
});

test('KEYB-11711: Shift+K resets; plain k is master tempo (KEYB-11383); Shift+P stays unbound; P cycles range', () => {
	assert.deepEqual(resolve(bareKey('k', { shiftKey: true })), {
		kind: 'tempo-reset',
		repeat: false
	});
	assert.deepEqual(resolve(bareKey('k')), {
		kind: 'recent-deck',
		verb: 'master-tempo',
		repeat: false
	});
	assert.deepEqual(resolve(bareKey('K')), {
		kind: 'recent-deck',
		verb: 'master-tempo',
		repeat: false
	});
	assert.deepEqual(resolve(bareKey('p')), { kind: 'pitch-range-cycle', repeat: false });
	assert.deepEqual(resolve(bareKey('P')), { kind: 'pitch-range-cycle', repeat: false });
	assert.equal(resolve(bareKey('P', { shiftKey: true })), null);
});

test('KEYB-11711: meta, ctrl, alt and text entry leave tempo keys alone', () => {
	for (const key of ['.', ',', 'p', 'P']) {
		assert.equal(resolve(bareKey(key, { metaKey: true })), null);
		assert.equal(resolve(bareKey(key, { ctrlKey: true })), null);
		assert.equal(resolve(bareKey(key, { altKey: true })), null);
		assert.equal(resolve(bareKey(key, { target: TEXT })), null);
	}
});

test('KEYB-11711: settings open and repeat semantics for tempo keys', () => {
	const calls = [];
	let prevented = 0;
	const handlers = {
		toggleRecentPlay() {},
		resizeLast() {},
		exitLast() {},
		armPinPlacement() {},
		waveZoomStep() {},
		tempoKey: (action) => calls.push(action)
	};
	const ev = (key, repeat, shiftKey = false) => ({
		...bareKey(key, { shiftKey, repeat }),
		timeStamp: 0,
		preventDefault: () => prevented++
	});
	assert.equal(
		routing.handlePerformanceShortcutKeydown(ev('.', false), handlers, { settingsOpen: true }),
		false
	);
	assert.equal(calls.length, 0);
	routing.handlePerformanceShortcutKeydown(ev('.', false), handlers, {});
	routing.handlePerformanceShortcutKeydown(ev('.', true), handlers, {});
	assert.equal(calls.length, 2);
	calls.length = 0;
	routing.handlePerformanceShortcutKeydown(ev('k', false, true), handlers, {});
	routing.handlePerformanceShortcutKeydown(ev('k', true, true), handlers, {});
	assert.deepEqual(calls, [{ kind: 'tempo-reset' }]);
	calls.length = 0;
	routing.handlePerformanceShortcutKeydown(ev('p', false), handlers, {});
	routing.handlePerformanceShortcutKeydown(ev('p', true), handlers, {});
	assert.deepEqual(calls, [{ kind: 'pitch-range-cycle' }]);
	assert.ok(prevented > 0);
});

test('wave-zoom shortcuts are unchanged after tempo keys landed', () => {
	assert.deepEqual(resolve(bareKey('[')), { kind: 'wave-zoom', direction: 'out' });
	assert.deepEqual(resolve(bareKey(']')), { kind: 'wave-zoom', direction: 'in' });
});

test('wiring: performance-hotkeys sends tempo keys through tempoKeyCommand', async () => {
	const source = await readFile('src/lib/rb/performance-hotkeys.ts', 'utf8');
	assert.match(source, /tempoKey: \(action\) => \{\s*void _tempoKey\(action\);/);
	assert.match(source, /tempoKeyCommand/);
	assert.match(source, /getRecentDeck\(\)/);
});

// ----- #10354 hot-cue digit keys -----

function digit(code, extra = {}) {
	return {
		code,
		key: code.replace('Digit', ''),
		target: PAGE,
		metaKey: false,
		ctrlKey: false,
		altKey: false,
		shiftKey: false,
		repeat: false,
		...extra
	};
}

const resolveCtx = (e, ctx = {}) => routing.resolvePerformanceShortcutAction(e, ctx);

test('Digit3 plain triggers hot cue slot C', () => {
	assert.deepEqual(resolveCtx(digit('Digit3')), {
		kind: 'hot-cue-trigger',
		slot: 'C',
		repeat: false
	});
});

test('Shift+Digit3 matches by code not key (Shift+# is still slot C save)', () => {
	assert.deepEqual(resolveCtx(digit('Digit3', { key: '#', shiftKey: true })), {
		kind: 'hot-cue-save',
		slot: 'C',
		repeat: false
	});
});

test('Shift+Digit1 save slot A even when key is !', () => {
	assert.deepEqual(resolveCtx(digit('Digit1', { key: '!', shiftKey: true })), {
		kind: 'hot-cue-save',
		slot: 'A',
		repeat: false
	});
});

test('Cmd/Ctrl+Shift+Digit3 clear slot C', () => {
	assert.deepEqual(resolveCtx(digit('Digit3', { metaKey: true, shiftKey: true })), {
		kind: 'hot-cue-clear',
		slot: 'C',
		repeat: false
	});
	assert.deepEqual(resolveCtx(digit('Digit3', { ctrlKey: true, shiftKey: true })), {
		kind: 'hot-cue-clear',
		slot: 'C',
		repeat: false
	});
});

test('Digit8 maps to H; Digit0 and Digit9 are not hot cues', () => {
	assert.deepEqual(resolveCtx(digit('Digit8')).slot, 'H');
	assert.equal(resolveCtx(digit('Digit9')), null);
	assert.equal(resolveCtx(digit('Digit0')), null);
});

test('Shift+0 still reaches loop-exit via key )', () => {
	assert.deepEqual(resolveCtx({ ...digit('Digit0'), key: ')', shiftKey: true }), {
		kind: 'loop-exit'
	});
});

test('dialSelected blocks all three hot cue chords', () => {
	const dial = { dialSelected: true };
	assert.equal(resolveCtx(digit('Digit1'), dial), null);
	assert.equal(resolveCtx(digit('Digit1', { shiftKey: true }), dial), null);
	assert.equal(resolveCtx(digit('Digit1', { metaKey: true, shiftKey: true }), dial), null);
});

test('altKey and text entry block hot cue digits', () => {
	assert.equal(resolveCtx(digit('Digit1', { altKey: true })), null);
	assert.equal(resolveCtx(digit('Digit1', { target: TEXT })), null);
});

test('Cmd+Digit2 without shift is not a hot cue (deck layout owns it)', () => {
	const action = resolveCtx(digit('Digit2', { metaKey: true, key: '2' }));
	assert.ok(action === null || action.kind !== 'hot-cue-trigger');
	assert.ok(action === null || action.kind !== 'hot-cue-save');
	assert.ok(action === null || action.kind !== 'hot-cue-clear');
});

test('repeat flag is carried on hot cue actions', () => {
	assert.deepEqual(resolveCtx(digit('Digit5', { repeat: true })), {
		kind: 'hot-cue-trigger',
		slot: 'E',
		repeat: true
	});
});

test('handlePerformanceShortcutKeydown: Digit5 triggers once, repeat swallowed', () => {
	const calls = [];
	let prevented = 0;
	const handlers = {
		toggleRecentPlay() {},
		resizeLast() {},
		exitLast() {},
		armPinPlacement() {},
		waveZoomStep() {},
		hotCueTrigger: (slot) => calls.push(slot)
	};
	const ev = (repeat) => ({
		...digit('Digit5'),
		repeat,
		timeStamp: 0,
		preventDefault: () => prevented++,
		stopPropagation: () => {}
	});
	assert.equal(routing.handlePerformanceShortcutKeydown(ev(false), handlers, {}), true);
	assert.equal(routing.handlePerformanceShortcutKeydown(ev(true), handlers, {}), true);
	assert.deepEqual(calls, ['E']);
	assert.equal(prevented, 2);
});

test('settingsOpen and dialSelected return false without preventDefault', () => {
	let prevented = 0;
	const handlers = {
		toggleRecentPlay() {},
		resizeLast() {},
		exitLast() {},
		armPinPlacement() {},
		waveZoomStep() {},
		hotCueTrigger: () => {
			throw new Error('must not run');
		}
	};
	const ev = {
		...digit('Digit1'),
		timeStamp: 0,
		preventDefault: () => prevented++
	};
	assert.equal(
		routing.handlePerformanceShortcutKeydown(ev, handlers, { settingsOpen: true }),
		false
	);
	assert.equal(prevented, 0);
	assert.equal(
		routing.handlePerformanceShortcutKeydown(ev, handlers, { dialSelected: true }),
		false
	);
	assert.equal(prevented, 0);
});

test('wiring: performance-hotkeys reads knob selection and filled-only trigger', async () => {
	const source = await readFile('src/lib/rb/performance-hotkeys.ts', 'utf8');
	assert.match(source, /dialSelected:\s*selectedKnobIds\(\)\.length > 0/);
	assert.match(
		source,
		/if \(filled\) await runPerformanceCommandFromUi\(\{ type: 'hot_cue_trigger'/
	);
});

// ----- #9455: a dial or fader selection owns = and -; a deck keeps loop resize -----

test('if a deck (not a dial) is selected then = doubles and - halves the loop; a dial selection yields both', () => {
	/** [if] no dial is selected [then] = + - _ resize the loop; with a dial selected they route nowhere, [else stop]. */
	const deck = (k) => routing.resolvePerformanceShortcutAction(bareKey(k), { dialSelected: false });
	const dial = (k) => routing.resolvePerformanceShortcutAction(bareKey(k), { dialSelected: true });
	assert.deepEqual(deck('='), { kind: 'loop-resize', factor: 2 });
	assert.deepEqual(deck('+'), { kind: 'loop-resize', factor: 2 });
	assert.deepEqual(deck('-'), { kind: 'loop-resize', factor: 0.5 });
	assert.deepEqual(deck('_'), { kind: 'loop-resize', factor: 0.5 });
	for (const k of ['=', '+', '-', '_']) assert.equal(dial(k), null, k);
});
