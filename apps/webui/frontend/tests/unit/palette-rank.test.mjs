// requirement: CMDK-9465
// if fixture top-1 hits fall below 18 of 20 then broken
// if frecency does not break equal fuzzy ties then broken
// if ranking 500 commands median is not under 2 ms then broken
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import {
	FRECENCY_HALF_LIFE_MS,
	PICKS_CAP,
	frecency,
	fuzzyScore,
	notePick,
	rankCommands
} from '../../src/lib/palette/rank.ts';
import { loadTypeScriptModule } from './load-typescript.mjs';

const FIXTURE = JSON.parse(
	readFileSync(new URL('./fixtures/palette-queries.json', import.meta.url), 'utf8')
);

let actions;
let prefsFields;

before(async () => {
	actions = await loadTypeScriptModule('src/lib/rb/cmdk-actions.ts');
	prefsFields = await loadTypeScriptModule('src/lib/rb/prefs-fields.ts');
});

test('fixture top-1 hits at least 18 of 20', (t) => {
	const { commands, queries } = FIXTURE;
	assert.equal(queries.length, 20);
	const misses = [];
	let hits = 0;
	for (const { query, expected } of queries) {
		const ranked = rankCommands(commands, query);
		const got = ranked[0]?.id;
		if (got === expected) hits++;
		else misses.push({ query, expected, got });
	}
	console.log(`palette top-1 hits: ${hits}/20`);
	t.diagnostic(`palette top-1 hits: ${hits}/20`);
	assert.ok(hits >= 18, `top-1 hits ${hits}/20 is below 18; misses: ${JSON.stringify(misses)}`);
});

test('frecency breaks equal fuzzy ties', (t) => {
	const items = [
		{ id: 'eject-1', label: 'Eject deck 1' },
		{ id: 'eject-2', label: 'Eject deck 2' }
	];
	const s1 = fuzzyScore('eject', items[0].label);
	const s2 = fuzzyScore('eject', items[1].label);
	assert.equal(s1, s2);
	assert.deepEqual(
		rankCommands(items, 'eject').map((i) => i.id),
		['eject-1', 'eject-2']
	);
	const now = Date.now();
	const picks = notePick(notePick([], 'eject-2', now - 50 * 60_000), 'eject-2', now - 20 * 60_000);
	const ranked = rankCommands(items, 'eject', { picks, now });
	assert.equal(ranked[0]?.id, 'eject-2');
	const w = frecency(picks[0], now);
	assert.ok(w >= 1.9 && w <= 2);
	t.diagnostic(`frecency weight after two picks: ${w}`);
});

test('rankCommands median under 2 ms for 500 items', (t) => {
	const now = Date.now();
	const catalog = FIXTURE.commands;
	const items = [];
	for (let i = 0; i < 500; i++) {
		const c = catalog[i % catalog.length];
		items.push({ id: `${c.id}-${i}`, label: `${c.label} ${i}`, keywords: c.keywords });
	}
	let picks = [];
	for (let i = 0; i < 50; i++) picks = notePick(picks, `loop-8-${i * 7}`, now - i * 60_000);
	for (let w = 0; w < 20; w++) rankCommands(items, 'lp8', { picks, now });
	const samples = [];
	for (let r = 0; r < 101; r++) {
		const t0 = performance.now();
		rankCommands(items, 'lp8', { picks, now });
		samples.push(performance.now() - t0);
	}
	samples.sort((a, b) => a - b);
	const median = samples[Math.floor(samples.length / 2)];
	t.diagnostic(`palette rank median ms: ${median}`);
	assert.ok(median < 2, `median ${median} ms is not under 2 ms`);
});

test('frecency decay at half-life', () => {
	const pick = { key: 'x', weight: 1, at: 0 };
	const half = frecency(pick, FRECENCY_HALF_LIFE_MS);
	assert.ok(Math.abs(half - 0.5) < 1e-9);
	const future = frecency(pick, -1000);
	assert.ok(future <= pick.weight);
});

test('notePick caps and does not mutate input', () => {
	const now = 1_000_000;
	const base = [];
	let picks = base;
	for (let i = 0; i < PICKS_CAP + 5; i++) {
		picks = notePick(picks, `k-${i}`, now + i);
	}
	assert.equal(picks.length, PICKS_CAP);
	assert.equal(picks[0]?.key, `k-${PICKS_CAP + 4}`);
	assert.equal(base.length, 0);
});

test('fuzzyScore shape', () => {
	assert.equal(fuzzyScore('zzz', 'abc'), null);
	assert.ok(fuzzyScore('SY', 'Beat sync') > fuzzyScore('sy', 'easy'));
	const scattered = fuzzyScore('lp', 'loop 8');
	const run = fuzzyScore('lo', 'loop');
	assert.ok(run > scattered);
	assert.ok(fuzzyScore('m', 'AutoMix') >= fuzzyScore('m', 'automix'));
});

test('empty query orders by frecency then input order', () => {
	const items = [
		{ id: 'a', label: 'Alpha' },
		{ id: 'b', label: 'Beta' }
	];
	assert.deepEqual(
		rankCommands(items, '').map((i) => i.id),
		['a', 'b']
	);
	const now = Date.now();
	const picks = notePick([], 'b', now);
	assert.deepEqual(
		rankCommands(items, '', { picks, now }).map((i) => i.id),
		['b', 'a']
	);
});

test('cmdk-actions ranked path', () => {
	assert.equal(
		actions.commandKey({
			id: 'eject-2:9',
			kind: 'command',
			label: '',
			command: { type: 'eject', deck: 2 }
		}),
		'eject-2'
	);
	const cmds = [
		{
			kind: 'command',
			id: 'eject-2:1',
			label: 'Eject deck 2',
			command: { type: 'eject', deck: 2 }
		},
		{ kind: 'command', id: 'eject-1:1', label: 'Eject deck 1', command: { type: 'eject', deck: 1 } }
	];
	const ranked = rankCommands(
		cmds.map((c) => ({ id: c.id.split(':')[0], label: c.label })),
		'ej 2'
	);
	const order = ranked.map((r) =>
		`eject-${r.id.replace('eject-', '')}:1`.replace('eject-eject', 'eject')
	);
	const layout = actions.layoutGroups({
		query: 'ej 2',
		recent: [],
		playlist: { rows: [], total: 0 },
		collection: { rows: [], total: 0 },
		commands: cmds.filter((c) => ranked.some((r) => r.id === c.id.split(':')[0])),
		settings: [],
		commandsRanked: true
	});
	const cmdIds = layout.groups
		.flatMap((g) => g.entries)
		.filter((e) => e.kind === 'action')
		.map((e) => e.action.id);
	assert.ok(cmdIds.includes('eject-2:1'));
	assert.deepEqual(actions.actionRows('?', cmds, []), []);
});

test('parseCmdkCommandPicks', () => {
	const key = 'mdt.rb.ui-prefs.v1';
	assert.deepEqual(prefsFields.parseCmdkCommandPicks(undefined, key), []);
	const round = [{ key: 'open-settings', weight: 1.5, at: 100 }];
	assert.deepEqual(prefsFields.parseCmdkCommandPicks(round, key), round);
	assert.throws(() => prefsFields.parseCmdkCommandPicks({}, key), /cmdk_command_picks/);
	assert.throws(
		() => prefsFields.parseCmdkCommandPicks([{ key: '', weight: 1, at: 1 }], key),
		/cmdk_command_picks/
	);
});
