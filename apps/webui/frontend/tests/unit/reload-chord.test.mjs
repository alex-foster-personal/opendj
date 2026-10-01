// requirement: LIBUX-29
// [if] Cmd+R is pressed in a browser tab [then] the page leaves it to the
// browser, so a refresh reloads instead of toggling technically-working mode
//
// Regression lines:
// - if a browser tab claims Cmd+R then a refresh hides the whole UI instead
//   of reloading it
// - if the desktop shell stops claiming Cmd+R then LIBUX-05's toggle is gone
//   from the one place it has a transparent window to work in
// - if a browser tab stops claiming Ctrl+R then overlay mode has no keyboard
//   door in a tab at all (the opposite overshoot)

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/reload-chord.ts');
});

const CMD_R = { metaKey: true, ctrlKey: false };
const CTRL_R = { metaKey: false, ctrlKey: true };

test('a browser tab leaves Cmd+R to the browser', () => {
	assert.equal(mod.pageOwnsReloadChord(CMD_R, null), false);
});

test('a browser tab still owns Ctrl+R, so overlay mode keeps a keyboard door there', () => {
	assert.equal(mod.pageOwnsReloadChord(CTRL_R, null), true);
});

test('a desktop shell owns both chords', () => {
	for (const shell of ['electron', 'tauri']) {
		assert.equal(mod.pageOwnsReloadChord(CMD_R, shell), true, `${shell} Cmd+R`);
		assert.equal(mod.pageOwnsReloadChord(CTRL_R, shell), true, `${shell} Ctrl+R`);
	}
});

test('the hotkey handler asks before it prevents the default, using the real shell probe', async () => {
	const hotkeys = await readFile('src/lib/rb/technically-working-hotkeys.ts', 'utf8');
	const chordBranch = hotkeys.slice(
		hotkeys.indexOf("if (_isModChord(e, 'r')) {"),
		hotkeys.indexOf("} else if (_isModChord(e, 'e')) {")
	);
	assert.ok(chordBranch.length > 0, 'the R chord branch must exist');
	const guard = chordBranch.indexOf('if (!pageOwnsReloadChord(e, nativeShellKind())) return;');
	const prevent = chordBranch.indexOf('e.preventDefault();');
	assert.ok(guard !== -1, 'the R chord branch must consult pageOwnsReloadChord');
	assert.ok(prevent !== -1 && guard < prevent, 'the guard must run before preventDefault');
});
