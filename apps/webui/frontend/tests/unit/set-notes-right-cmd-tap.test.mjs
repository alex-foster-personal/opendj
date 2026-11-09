/**
 * SETNOTE-6340b: a lone right-Cmd / right-Ctrl tap toggles the Set notes pad.
 *
 * [if] right Cmd or right Ctrl is pressed and released alone [then] it is a
 * tap; a chord, a left-hand modifier, or a blur in between is not
 */

import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let tap;
let registry;

let deckKey;

before(async () => {
	tap = await loadTypeScriptModule('src/lib/set-notes/hotkey/right-cmd-tap.ts');
	deckKey = await loadTypeScriptModule('src/lib/set-notes/hotkey/deck-key.ts');
	registry = await loadTypeScriptModule('src/lib/components/rb/hotkeys/hotkeys-registry.ts');
});

function press(detector, ...codes) {
	for (const code of codes) detector.keyDown({ code });
	return codes
		.slice()
		.reverse()
		.map((code) => detector.keyUp({ code }));
}

test('the exported codes are the right-hand modifiers', () => {
	assert.equal(tap.RIGHT_CMD_CODE, 'MetaRight');
	assert.equal(tap.RIGHT_CTRL_CODE, 'ControlRight');
});

test('a MetaRight tap fires', () => {
	const d = tap.createRightCmdTapDetector();
	assert.deepEqual(press(d, 'MetaRight'), [true]);
});

test('a ControlRight tap fires', () => {
	const d = tap.createRightCmdTapDetector();
	assert.deepEqual(press(d, 'ControlRight'), [true]);
});

test('MetaRight+KeyC is a chord, not a tap', () => {
	const d = tap.createRightCmdTapDetector();
	assert.deepEqual(press(d, 'MetaRight', 'KeyC'), [false, false]);
});

test('macOS dropping the KeyC keyup under Cmd does not stick the next tap', () => {
	const d = tap.createRightCmdTapDetector();
	d.keyDown({ code: 'MetaRight' });
	d.keyDown({ code: 'KeyC' });
	assert.equal(d.keyUp({ code: 'MetaRight' }), false);
	assert.deepEqual(press(d, 'MetaRight'), [true]);
});

test('left-hand modifiers never arm', () => {
	const d = tap.createRightCmdTapDetector();
	assert.deepEqual(press(d, 'MetaLeft'), [false]);
	assert.deepEqual(press(d, 'ControlLeft'), [false]);
});

test('right Cmd pressed while another key is held does not arm', () => {
	const d = tap.createRightCmdTapDetector();
	d.keyDown({ code: 'ShiftLeft' });
	d.keyDown({ code: 'MetaRight' });
	assert.equal(d.keyUp({ code: 'MetaRight' }), false);
});

test('SETNOTE-9940: a left+right pair of the same modifier is a chord, either order', () => {
	const d = tap.createRightCmdTapDetector();
	assert.deepEqual(press(d, 'MetaLeft', 'MetaRight'), [false, false]);
	assert.deepEqual(press(d, 'ControlLeft', 'ControlRight'), [false, false]);
	assert.deepEqual(press(d, 'MetaRight', 'MetaLeft'), [false, false]);
	assert.deepEqual(press(d, 'ControlRight', 'ControlLeft'), [false, false]);
});

test('SETNOTE-9940: holding left Cmd and tapping right Cmd never toggles', () => {
	const d = tap.createRightCmdTapDetector();
	d.keyDown({ code: 'MetaLeft' });
	d.keyDown({ code: 'MetaRight' });
	assert.equal(d.keyUp({ code: 'MetaRight' }), false);
});

test('any chord with a right modifier never completes a tap', () => {
	const d = tap.createRightCmdTapDetector();
	assert.deepEqual(press(d, 'MetaLeft', 'MetaRight', 'KeyC'), [false, false, false]);
	assert.deepEqual(press(d, 'ControlLeft', 'MetaRight'), [false, false]);
	assert.deepEqual(press(d, 'MetaRight', 'ControlRight'), [false, false]);
});

test('a lone tap still fires after a rejected chord', () => {
	const d = tap.createRightCmdTapDetector();
	press(d, 'MetaLeft', 'MetaRight');
	assert.deepEqual(press(d, 'MetaRight'), [true]);
	press(d, 'ControlLeft', 'ControlRight');
	assert.deepEqual(press(d, 'ControlRight'), [true]);
});

test('a blur between keydown and keyup disarms', () => {
	const d = tap.createRightCmdTapDetector();
	d.keyDown({ code: 'MetaRight' });
	d.blur();
	assert.equal(d.keyUp({ code: 'MetaRight' }), false);
});

test('auto-repeat keydowns of the held modifier keep it armed', () => {
	const d = tap.createRightCmdTapDetector();
	d.keyDown({ code: 'MetaRight' });
	d.keyDown({ code: 'MetaRight', repeat: true });
	assert.equal(d.keyUp({ code: 'MetaRight' }), true);
});

test('the hotkeys cheatsheet lists the Set notes tap', () => {
	const row = registry.HOTKEY_REGISTRY.find((entry) => entry.id === 'set-notes-toggle');
	assert.ok(row, 'set-notes-toggle must be in HOTKEY_REGISTRY');
	assert.equal(row.chord, registry.SET_NOTES_TAP_CHORD);
	assert.match(row.chord, /Right Cmd/);
});

const PLAIN = { repeat: false, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false };

test('SETNOTE-9940: an unmodified digit counts as a deck key', () => {
	assert.equal(deckKey.isPlainDeckKey(PLAIN), true);
});

test('SETNOTE-9940: Shift is a modifier, so Shift+digit stays ordinary input', () => {
	assert.equal(deckKey.isPlainDeckKey({ ...PLAIN, shiftKey: true }), false);
});

test('Cmd, Ctrl, Alt and repeat each keep the digit ordinary input', () => {
	for (const field of ['metaKey', 'ctrlKey', 'altKey', 'repeat']) {
		assert.equal(deckKey.isPlainDeckKey({ ...PLAIN, [field]: true }), false, field);
	}
});
