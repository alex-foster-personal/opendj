// requirement: CMDK-13797
/**
 * #13693: every keymap and performance registry command is a Cmd K row, run by id
 * through the keyboard agent handle (#13690), the same call an agent makes.
 *
 * Regression lines:
 * - if a KEYMAP_COMMANDS or registry id is missing from the rows, or shows twice, then broken
 * - if a hand-written palette row or an earlier bar row id gets a second keyboard row then broken
 * - if running a row does not call the by-id runner with its id, or hides a refusal, then broken
 * - if a row's hint is not the active preset's chord then broken
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const rowsModule = await loadTypeScriptModule('src/lib/palette/keyboard-command-rows.ts');
const handleModule = await loadTypeScriptModule('src/lib/keyboard/keyboard-agent-handle.ts');
const keymapModule = await loadTypeScriptModule('src/lib/keyboard/keymap.ts');
const commandsModule = await loadTypeScriptModule('src/lib/keyboard/keymap-commands.ts');
const registryModule = await loadTypeScriptModule('src/lib/keyboard/command-registry.ts');
const paletteModule = await loadTypeScriptModule('src/lib/palette/commands.ts');

function handleFor(presetId, runs = []) {
	const keymap = keymapModule.getKeymapPreset(presetId);
	return handleModule.createKeyboardAgentHandle({
		commands: commandsModule.KEYMAP_COMMANDS,
		activeKeymap: () => keymap,
		runKeymapTarget: (commandId) => runs.push(commandId)
	});
}

const allIds = () => [
	...commandsModule.KEYMAP_COMMANDS.map((cmd) => cmd.id),
	...registryModule.listPerformanceCommandMetadata().map((def) => def.id)
];

test('every keymap and registry command id is exactly one row', () => {
	/** [if] the palette rows are built [then] every KEYMAP_COMMANDS and registry id appears exactly once, [else stop]. */
	const rows = rowsModule.keyboardCommandRows(handleFor('rekordbox'), []);
	const ids = rows.map((row) => row.id);
	assert.equal(new Set(ids).size, ids.length, 'no id shows twice');
	const handWritten = new Set(
		paletteModule.PALETTE_REGISTRY.commands.filter((c) => c.run !== null).map((c) => c.id)
	);
	assert.deepEqual(
		ids,
		allIds().filter((id) => !handWritten.has(id))
	);
	assert.ok(ids.length > 200, `rows cover the keymap and registry (${ids.length})`);
	for (const row of rows) {
		assert.equal(row.kind, 'command');
		assert.deepEqual(row.command, { type: 'keyboard', id: row.id });
		assert.ok(row.label.length > 0 && row.section.length > 0, `${row.id} has label and section`);
	}
	assert.equal(rows.find((row) => row.id === 'mixer.crossfader_left').section, 'Mixer');
	assert.equal(rows.find((row) => row.id === 'selector.drill-in').section, 'Selector');
	assert.deepEqual(
		rowsModule.keyboardCommandRows(null, []),
		[],
		'no handle installed offers no rows'
	);
});

test('an id a hand-written or earlier bar row covers gets no second row', () => {
	/** [if] a hand-written palette row already covers a command id [then] no duplicate row is added, [else stop]. */
	const covered = {
		kind: 'command',
		id: 'dial.position.5',
		label: 'Dial to 5',
		hint: '5',
		command: { type: 'performance', id: 'dial.position.5' }
	};
	const listed = [
		{ id: 'deck.play', label: 'Play', scope: 'deck', source: 'registry', chords: [] },
		{ id: 'dial.position.5', label: 'Dial to 5', scope: 'dial', source: 'registry', chords: ['5'] },
		{ id: 'selector.drill-in', label: 'Drill in', scope: 'global', source: 'registry', chords: [] }
	];
	assert.ok(
		paletteModule.PALETTE_REGISTRY.commands.some((c) => c.id === 'deck.play' && c.run !== null)
	);
	const runner = { list: () => listed, run: () => assert.fail('not run') };
	const ids = rowsModule.keyboardCommandRows(runner, [covered]).map((row) => row.id);
	assert.deepEqual(ids, ['selector.drill-in']);
});

test('running a row calls the shared by-id runner with that id and surfaces a refusal', () => {
	/** [if] a row is run [then] it calls the shared by-id runner with that id and a refusal reaches the bar, [else stop]. */
	const runs = [];
	const handle = handleFor('rekordbox', runs);
	const row = rowsModule.keyboardCommandRows(handle, []).find((r) => r.id === 'deck.1.play');
	assert.equal(rowsModule.runKeyboardCommandRow(handle, row), null);
	assert.deepEqual(runs, ['deck.1.play']);
	const calls = [];
	const refusing = {
		list: () => [],
		run: (id) => (calls.push(id), { ok: false, id, refusal: 'no_selector' })
	};
	const selector = {
		label: 'Move selector up',
		command: { type: 'keyboard', id: 'selector.move.up' }
	};
	assert.equal(
		rowsModule.runKeyboardCommandRow(refusing, selector),
		'Move selector up did not run (no_selector)'
	);
	assert.deepEqual(calls, ['selector.move.up']);
	assert.equal(
		rowsModule.runKeyboardCommandRow(null, selector),
		'Move selector up did not run (no_keyboard_runtime)'
	);
});

test('a command bound in the active preset shows that chord as its hint', () => {
	/** [if] a command has an active chord in the current preset [then] its row shows that chord as the hint, [else stop]. */
	const hint = (preset, id) =>
		rowsModule.keyboardCommandRows(handleFor(preset), []).find((r) => r.id === id).hint;
	assert.equal(hint('rekordbox', 'deck.1.play'), 'Z');
	assert.equal(hint('rekordbox', 'deck.1.hot_cue_clear.A'), 'Cmd Shift 1');
	assert.equal(hint('serato', 'deck.1.play'), 'W', 'switching preset switches the hint');
	assert.equal(hint('rekordbox', 'dial.jump.5'), 'Cmd 5');
	assert.equal(hint('rekordbox', 'selector.drill-in'), 'E');
	assert.equal(hint('rekordbox', 'preset.run.bass-swap-8'), '', 'an unbound command has no hint');
});
