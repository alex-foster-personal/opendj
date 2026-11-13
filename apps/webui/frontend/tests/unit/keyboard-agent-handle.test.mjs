// requirement: KEYB-13762
/**
 * #13690: `window.__odjKeyboard` lists and runs any keyboard command by id for agents.
 * Selector geometry below is synthesized for this test.
 *
 * Regression lines:
 * - if list() omits a keymap or registry command, or is not plain JSON, then broken
 * - if run('deck.1.play') does not reach the runner a chord press uses then broken
 * - if run('selector.move.right') does not return the live selector's new id then broken
 * - if run('no.such.command') does not throw naming the id then broken
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { createSelectorTree } from '../../src/lib/keyboard/selector-tree.ts';
import { loadTypeScriptModule } from './load-typescript.mjs';

const handleModule = await loadTypeScriptModule('src/lib/keyboard/keyboard-agent-handle.ts');
const keymapModule = await loadTypeScriptModule('src/lib/keyboard/keymap.ts');
const commandsModule = await loadTypeScriptModule('src/lib/keyboard/keymap-commands.ts');
const registryModule = await loadTypeScriptModule('src/lib/keyboard/command-registry.ts');

const rekordbox = keymapModule.getKeymapPreset('rekordbox');

function handleWith(runs) {
	return handleModule.createKeyboardAgentHandle({
		commands: commandsModule.KEYMAP_COMMANDS,
		activeKeymap: () => rekordbox,
		runKeymapTarget: (commandId, pressT0Ms, legacy) => runs.push({ commandId, legacy })
	});
}

function selectorTree() {
	const node = (id, x) => ({
		id,
		parentId: null,
		kind: 'section',
		rect: { x, y: 0, width: 100, height: 100 }
	});
	return createSelectorTree([node('left', 0), node('right', 200)], 'left');
}

test('list() returns every keymap and registry id with label and active chords as plain JSON', () => {
	/** [if] list() is called [then] every keymap and registry id comes back with label and chords as plain JSON, [else stop]. */
	const listed = handleWith([]).list();
	assert.deepEqual(JSON.parse(JSON.stringify(listed)), listed);
	const ids = listed.map((row) => row.id);
	assert.equal(new Set(ids).size, ids.length, 'ids are unique across keymap and registry');
	const expected = [
		...commandsModule.KEYMAP_COMMANDS.map((cmd) => cmd.id),
		...registryModule.listPerformanceCommandMetadata().map((def) => def.id)
	];
	assert.deepEqual(ids, expected);
	for (const row of listed) assert.ok(row.label.length > 0, `${row.id} has a label`);
	const play = listed.find((row) => row.id === 'deck.1.play');
	assert.deepEqual(play, {
		id: 'deck.1.play',
		label: 'Deck 1 play toggle',
		scope: 'deck',
		source: 'keymap',
		chords: ['Z']
	});
});

test("run('deck.1.play') reaches the same target runner its chord press does", () => {
	/** [if] run('deck.1.play') is called [then] the chord-press target runner gets deck.1.play, [else stop]. */
	const runs = [];
	const pressed = keymapModule.resolveKeymapKeydown(
		rekordbox,
		{ code: 'KeyZ', metaKey: false, ctrlKey: false, altKey: false, shiftKey: false },
		{ platform: 'MacIntel', activeScopes: ['global', 'deck'] }
	);
	assert.equal(pressed, 'deck.1.play');
	assert.deepEqual(handleWith(runs).run('deck.1.play'), {
		ok: true,
		id: 'deck.1.play',
		selected: null
	});
	assert.deepEqual(runs, [{ commandId: pressed, legacy: null }]);
});

test("run('selector.move.right') with a live selector runs the registry command and returns the selected id", () => {
	/** [if] a selector is live and run('selector.move.right') is called [then] it returns the newly selected id, [else stop]. */
	const handle = handleWith([]);
	handleModule.setKeyboardSelectorProvider(() => null);
	assert.deepEqual(handle.run('selector.move.right'), {
		ok: false,
		id: 'selector.move.right',
		refusal: 'no_selector'
	});
	const tree = selectorTree();
	handleModule.setKeyboardSelectorProvider(() => tree);
	assert.deepEqual(handle.run('selector.move.right'), {
		ok: true,
		id: 'selector.move.right',
		selected: 'right'
	});
	assert.equal(tree.selected(), 'right');
	handleModule.setKeyboardSelectorProvider(null);
});

test("run('no.such.command') throws an error naming the id", () => {
	/** [if] run('no.such.command') is called [then] it throws an error naming no.such.command, [else stop]. */
	assert.throws(() => handleWith([]).run('no.such.command'), /no\.such\.command/);
});
