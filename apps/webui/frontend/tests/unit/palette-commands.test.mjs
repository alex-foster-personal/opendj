// requirement: CMDK-9600
/**
 * Palette command registry (issue #9464): menu ids, hotkeys, unique titles, and bar rows.
 *
 * Regression lines:
 * - if a MENU_COMMANDS id has neither a palette entry nor an exclusion then broken
 * - if a hotkey has no palette entry with its chord then broken
 * - if two palette entries share a title then broken
 * - if palette rows duplicate eject or open settings then broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { performanceCommandTypes } from './performance-command-types.mjs';

let palette;
let commands;
let hotkeys;
let actions;

before(async () => {
	palette = await loadTypeScriptModule('src/lib/palette/commands.ts');
	commands = await loadTypeScriptModule('src/lib/context-menu/commands.ts');
	hotkeys = await loadTypeScriptModule('src/lib/components/rb/hotkeys/hotkeys-registry.ts');
	actions = await loadTypeScriptModule('src/lib/rb/cmdk-actions.ts');
});

function menuCoverage(menuIds, registryCommands, exclusions) {
	const missing = [];
	const both = [];
	for (const id of menuIds) {
		const entry = registryCommands.find((c) => c.menu === id);
		const excluded = exclusions[id];
		if (entry !== undefined && excluded !== undefined) both.push(id);
		if (entry === undefined && (excluded === undefined || excluded.length < 20)) missing.push(id);
	}
	return { missing, both };
}

const PALETTE_DECKS = (target, decks) => ({ target, decks });

test('every MENU_COMMANDS id has a palette entry or an exclusion with reason', () => {
	const menuIds = Object.keys(commands.MENU_COMMANDS);
	const { missing, both } = menuCoverage(
		menuIds,
		palette.PALETTE_REGISTRY.commands,
		palette.PALETTE_REGISTRY.exclusions
	);
	assert.deepEqual(both, []);
	assert.deepEqual(missing, []);
	for (const key of Object.keys(palette.PALETTE_REGISTRY.exclusions)) {
		assert.ok(key in commands.MENU_COMMANDS, `exclusion key ${key} is not a menu id`);
	}
	const incomplete = { ...palette.PALETTE_REGISTRY.exclusions };
	delete incomplete['deck.load'];
	const neg = menuCoverage(menuIds, palette.PALETTE_REGISTRY.commands, incomplete);
	assert.ok(neg.missing.includes('deck.load'));
});

test('every hotkey action has a palette entry showing its shortcut', () => {
	for (const hk of hotkeys.HOTKEY_REGISTRY) {
		const entry = palette.PALETTE_REGISTRY.commands.find((c) => c.hotkey === hk.id);
		assert.ok(entry !== undefined, `missing palette entry for hotkey ${hk.id}`);
		assert.equal(entry.shortcut, hk.chord, `${entry.id} shortcut`);
	}
	for (const entry of palette.PALETTE_REGISTRY.commands) {
		if (entry.hotkey === null) continue;
		const hk = hotkeys.HOTKEY_REGISTRY.find((h) => h.id === entry.hotkey);
		assert.ok(hk !== undefined, `${entry.id} names unknown hotkey ${entry.hotkey}`);
	}
});

test('no two palette entries share a title', () => {
	const titles = new Map();
	const ids = new Set();
	for (const entry of palette.PALETTE_REGISTRY.commands) {
		const key = entry.title.trim().toLowerCase();
		assert.ok(
			!titles.has(key),
			`duplicate title ${entry.title} (${titles.get(key)} and ${entry.id})`
		);
		titles.set(key, entry.id);
		assert.ok(!ids.has(entry.id), `duplicate id ${entry.id}`);
		ids.add(entry.id);
	}
});

test('palette ipc types sit in PerformanceCommand and routes match describeCommand', () => {
	const perfTypes = performanceCommandTypes();
	for (const entry of palette.PALETTE_REGISTRY.commands) {
		if (entry.menu !== null) {
			assert.equal(entry.route, commands.describeCommand(entry.menu));
		}
		if (entry.run === null) {
			assert.ok(entry.waits && entry.waits.length >= 10, `${entry.id} missing waits`);
		}
		for (const arg of entry.args) {
			assert.ok(['deck', 'cue_slot', 'beats', 'value'].includes(arg.kind));
		}
		if (entry.menu !== null && entry.run?.kind === 'bar') {
			const ctx = PALETTE_DECKS(2, [
				{
					id: 1,
					loaded: false,
					playing: false,
					is_master: false,
					beat_sync_enabled: false,
					load_generation: 0,
					title: null
				},
				{
					id: 2,
					loaded: true,
					playing: false,
					is_master: false,
					beat_sync_enabled: false,
					load_generation: 3,
					title: 'T'
				},
				{
					id: 3,
					loaded: false,
					playing: false,
					is_master: false,
					beat_sync_enabled: false,
					load_generation: 0,
					title: null
				},
				{
					id: 4,
					loaded: false,
					playing: false,
					is_master: false,
					beat_sync_enabled: false,
					load_generation: 0,
					title: null
				}
			]);
			const deck = ctx.decks.find((d) => d.id === 2);
			const built = entry.run.build(ctx, deck ?? null);
			if (built.via === 'ipc') {
				const menuCmd = commands.MENU_COMMANDS[entry.menu];
				if (menuCmd.via === 'ipc') {
					assert.equal(built.command.type, menuCmd.type, entry.id);
				}
				assert.ok(
					perfTypes.has(built.command.type),
					`${entry.id} builds unknown type ${built.command.type}`
				);
			}
		}
	}
});

test('paletteCommandRows respects target deck and query rules', () => {
	const baseDecks = [
		{
			id: 1,
			loaded: true,
			playing: false,
			is_master: true,
			beat_sync_enabled: false,
			load_generation: 1,
			title: 'A'
		},
		{
			id: 2,
			loaded: true,
			playing: false,
			is_master: false,
			beat_sync_enabled: false,
			load_generation: 7,
			title: 'B'
		},
		{
			id: 3,
			loaded: false,
			playing: false,
			is_master: false,
			beat_sync_enabled: false,
			load_generation: 0,
			title: null
		},
		{
			id: 4,
			loaded: false,
			playing: false,
			is_master: false,
			beat_sync_enabled: false,
			load_generation: 0,
			title: null
		}
	];
	const ctx = PALETTE_DECKS(2, baseDecks);
	assert.deepEqual(actions.paletteCommandRows(ctx, ''), []);
	const unloaded = PALETTE_DECKS(3, baseDecks);
	assert.ok(!actions.paletteCommandRows(unloaded, 'play').some((r) => r.id.includes('@3')));
	const paused = actions.paletteCommandRows(ctx, 'play');
	const playRow = paused.find((r) => r.label === 'Play/pause deck 2');
	assert.ok(playRow);
	assert.equal(playRow.hint, 'Space');
	assert.ok(playRow.id.endsWith('=true'));
	const playingCtx = PALETTE_DECKS(
		2,
		baseDecks.map((d) => (d.id === 2 ? { ...d, playing: true } : d))
	);
	const playOn = actions.paletteCommandRows(playingCtx, 'play');
	const playOnRow = playOn.find((r) => r.label === 'Play/pause deck 2');
	assert.ok(playOnRow?.id.endsWith('=false'));
	assert.ok(!paused.some((r) => r.label === 'Eject deck' || r.label.startsWith('Open settings')));
	assert.ok(!paused.some((r) => r.id.startsWith('palette:hot_cue.')));
	const masterCtx = PALETTE_DECKS(1, baseDecks);
	assert.ok(
		!actions
			.paletteCommandRows(masterCtx, 'master')
			.some((r) => r.id.startsWith('palette:deck.master'))
	);
	const undoRows = actions.actionRows('undo', actions.paletteCommandRows(ctx, 'undo'), [], 20);
	assert.ok(undoRows.some((r) => r.kind === 'command' && r.label === 'Undo playlist change'));
	const revertRows = actions.actionRows(
		'revert',
		actions.paletteCommandRows(ctx, 'revert'),
		[],
		20
	);
	assert.ok(revertRows.some((r) => r.kind === 'command' && r.label === 'Undo playlist change'));
});

test('commandCandidates eject rows unchanged', () => {
	const DECKS = [
		{ id: 1, loaded: true, playing: true, title: 'Copper Lantern', load_generation: 3 },
		{ id: 2, loaded: true, playing: false, title: 'Night Ferry', load_generation: 7 },
		{ id: 3, loaded: false, playing: false, title: null, load_generation: 0 },
		{ id: 4, loaded: false, playing: false, title: null, load_generation: 0 }
	];
	const ids = actions.commandCandidates(DECKS, 'ej').map((c) => c.id);
	assert.deepEqual(ids, ['eject-1:3', 'eject-2:7', 'open-settings']);
});
