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

before(async () => {
	tap = await loadTypeScriptModule('src/lib/set-notes/hotkey/right-cmd-tap.ts');
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

test('an injected left+right twin pair is still one tap, either order', () => {
	const d = tap.createRightCmdTapDetector();
	d.keyDown({ code: 'ControlLeft' });
	d.keyDown({ code: 'ControlRight' });
	assert.equal(d.keyUp({ code: 'ControlLeft' }), false);
	assert.equal(d.keyUp({ code: 'ControlRight' }), true);
	assert.deepEqual(press(d, 'MetaRight', 'MetaLeft'), [false, true]);
});

test('the twin pair plus any other key is still a chord', () => {
	const d = tap.createRightCmdTapDetector();
	assert.deepEqual(press(d, 'MetaLeft', 'MetaRight', 'KeyC'), [false, false, false]);
	assert.deepEqual(press(d, 'ControlLeft', 'MetaRight'), [false, false]);
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
