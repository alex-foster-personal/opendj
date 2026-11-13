/**
 * Palette exclusions stay non-commandable (issue #13194).
 *
 * `PALETTE_REGISTRY.exclusions` gives each context-menu id the palette does not run a text
 * reason ((private path), "Implementation"). A reason alone is not
 * enough: searching the bar for an excluded action must offer no row that runs it.
 *
 * Regression lines:
 * - if an excluded deck context-menu id is searched and a bar row runs it then broken
 * - if an excluded track grid-edit id is searched and a bar row runs it then broken
 * - if a non-excluded palette command is searched and no bar row runs it then broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let palette;
let commands;
let actions;
let rank;

before(async () => {
	palette = await loadTypeScriptModule('src/lib/palette/commands.ts');
	commands = await loadTypeScriptModule('src/lib/context-menu/commands.ts');
	actions = await loadTypeScriptModule('src/lib/rb/cmdk-actions.ts');
	rank = await loadTypeScriptModule('src/lib/palette/rank.ts');
});

// Four loaded decks; deck 2 is the target and is not master, so every availability gate is open.
const CTX = {
	target: 2,
	decks: [1, 2, 3, 4].map((id) => ({
		id,
		loaded: true,
		playing: false,
		is_master: id === 1,
		beat_sync_enabled: false,
		load_generation: id * 3,
		title: `Track ${id}`
	}))
};

// Excluded deck context-menu ids and the words a DJ would type for them.
const DECK_QUERIES = {
	'deck.move': ['deck.move', 'move track to deck'],
	'deck.instant_double': ['deck.instant_double', 'instant double'],
	'deck.key_lock': ['deck.key_lock', 'key lock'],
	'deck.quantize': ['deck.quantize', 'quantize'],
	'deck.slip': ['deck.slip', 'slip'],
	'deck.track_info': ['deck.track_info', 'track info']
};

const GRID_QUERIES = ['track.grid_edit', 'grid edit', 'lock beatgrid'];

/** The palette bar's command rows for a query, as CommandBarV2 builds them (rows, then rank). */
function searchRows(query) {
	const rows = actions.paletteCommandRows(CTX, query);
	const ranked = rank.rankCommands(rows, query, { key: actions.commandKey });
	const filtered = actions.actionRows(query, rows, [], 1000);
	return [...ranked, ...filtered.filter((r) => r.kind === 'command')];
}

/** The registry entry a palette bar row came from (row ids are `palette:<entry id>@...`). */
function entryOf(row) {
	const entryId = row.id.slice('palette:'.length).split('@')[0].split('=')[0];
	const entry = palette.PALETTE_REGISTRY.commands.find((c) => c.id === entryId);
	assert.ok(entry !== undefined, `row ${row.id} has no registry entry`);
	return entry;
}

/** True when a bar row would run the given menu id, by binding or by the IPC it sends. */
function rowRuns(row, menuId) {
	if (entryOf(row).menu === menuId) return true;
	const menuCmd = commands.MENU_COMMANDS[menuId];
	const run = row.command.run;
	return menuCmd.via === 'ipc' && run.via === 'ipc' && run.command.type === menuCmd.type;
}

/** Assert every query for an excluded id offers no row that runs it. */
function assertNotCommandable(menuId, queries) {
	assert.ok(
		typeof palette.PALETTE_REGISTRY.exclusions[menuId] === 'string',
		`${menuId} is expected to be an exclusion`
	);
	assert.ok(
		!palette.PALETTE_REGISTRY.commands.some((c) => c.menu === menuId),
		`${menuId} is excluded but has a palette entry`
	);
	for (const query of queries) {
		const leaks = searchRows(query).filter((row) => rowRuns(row, menuId));
		assert.deepEqual(
			leaks.map((r) => r.id),
			[],
			`searching "${query}" offers a row that runs excluded ${menuId}`
		);
	}
}

test('excluded deck context-menu ids are absent from palette search', () => {
	// Each excluded deck menu id, searched by id and by its words, yields no runnable row.
	for (const [menuId, queries] of Object.entries(DECK_QUERIES)) {
		assertNotCommandable(menuId, queries);
	}
});

test('excluded track grid-edit id is absent from palette search', () => {
	// track.grid_edit, searched by id and by its words, yields no runnable row.
	assertNotCommandable('track.grid_edit', GRID_QUERIES);
	const route = commands.MENU_COMMANDS['track.grid_edit'];
	assert.equal(route.via, 'http');
	for (const query of GRID_QUERIES) {
		for (const row of searchRows(query)) {
			assert.notEqual(entryOf(row).route, commands.describeCommand('track.grid_edit'));
		}
	}
});

test('a non-excluded palette command is still offered by search', () => {
	// deck.master is not excluded: searching it offers a row that runs it, so the probe can see a hit.
	assert.equal(palette.PALETTE_REGISTRY.exclusions['deck.master'], undefined);
	for (const query of ['deck.master', 'make master']) {
		const hits = searchRows(query).filter((row) => rowRuns(row, 'deck.master'));
		assert.ok(hits.length > 0, `searching "${query}" offers no deck.master row`);
		assert.ok(hits.every((r) => r.label === 'Make master deck 2'));
		assert.deepEqual(hits[0].command.run, { via: 'ipc', command: { type: 'master', deck: 2 } });
	}
	const playHits = searchRows('play').filter((row) => rowRuns(row, 'deck.play'));
	assert.ok(playHits.some((r) => r.label === 'Play/pause deck 2'));
});
