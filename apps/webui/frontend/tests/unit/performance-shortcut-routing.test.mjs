/**
 * Registration guards only. Issue5761's behavior is exercised by the real
 * /performance browser in tests/live/performance-focused-controls.mjs.
 * Supersedes the FakeElement/handler stand-ins and fabricated feedback state
 * that could not prove native activation or unchanged real transport.
 *
 * KEYB-11711 regression lines:
 * - if , . fail to resolve to tempo-step then broken
 * - if plain k binds master tempo early then broken
 * - if text fields lose , . typing then broken
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

test('KEYB-11711: Shift+K resets; plain k and Shift+P stay unbound; P cycles range', () => {
	assert.deepEqual(resolve(bareKey('k', { shiftKey: true })), {
		kind: 'tempo-reset',
		repeat: false
	});
	assert.equal(resolve(bareKey('k')), null);
	assert.equal(resolve(bareKey('K')), null);
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
