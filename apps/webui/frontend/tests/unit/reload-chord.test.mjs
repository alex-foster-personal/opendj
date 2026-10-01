// requirement: LIBUX-29, LIBUX-31
// [if] Cmd+R is pressed anywhere, browser tab or desktop shell [then] the page
// leaves it to the host instead of toggling technically-working mode
//
// Regression lines:
// - if a browser tab claims Cmd+R then a refresh hides the whole UI instead
//   of reloading it
// - if the desktop shell claims Cmd+R then the packaged app hides its whole
//   UI on the reload chord and leaves a near-empty window (LIBUX-31)
// - if Ctrl+R stops being claimed then overlay mode has no keyboard door at
//   all (the opposite overshoot)

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

test('the page never claims Cmd+R, whatever hosts it', () => {
	assert.equal(mod.pageOwnsReloadChord(CMD_R), false);
});

test('the page still owns Ctrl+R, so overlay mode keeps a keyboard door', () => {
	assert.equal(mod.pageOwnsReloadChord(CTRL_R), true);
});

test('the answer does not depend on a shell probe', async () => {
	const source = await readFile('src/lib/rb/reload-chord.ts', 'utf8');
	assert.equal(mod.pageOwnsReloadChord.length, 1, 'one parameter: the chord');
	assert.equal(source.includes('native-shell'), false, 'no shell import to branch on');
});

test('the hotkeys overlay lists Ctrl+R for overlay mode and never Cmd+R', async () => {
	const registry = await loadTypeScriptModule('src/lib/components/rb/hotkeys/hotkeys-registry.ts');
	const entry = registry.HOTKEY_REGISTRY.find((e) => e.id === 'tech-mode-r');
	assert.ok(entry, 'the tech-mode-r entry must exist');
	assert.equal(entry.chord, 'Ctrl+R');
	assert.equal(
		registry.HOTKEY_REGISTRY.some((e) => e.chord === 'Cmd+R'),
		false,
		'no entry may advertise Cmd+R'
	);
});

test('the hotkey handler asks before it prevents the default', async () => {
	const hotkeys = await readFile('src/lib/rb/technically-working-hotkeys.ts', 'utf8');
	const chordBranch = hotkeys.slice(
		hotkeys.indexOf("if (_isModChord(e, 'r')) {"),
		hotkeys.indexOf("} else if (_isModChord(e, 'e')) {")
	);
	assert.ok(chordBranch.length > 0, 'the R chord branch must exist');
	const guard = chordBranch.indexOf('if (!pageOwnsReloadChord(e)) return;');
	const prevent = chordBranch.indexOf('e.preventDefault();');
	assert.ok(guard !== -1, 'the R chord branch must consult pageOwnsReloadChord');
	assert.ok(prevent !== -1 && guard < prevent, 'the guard must run before preventDefault');
});
