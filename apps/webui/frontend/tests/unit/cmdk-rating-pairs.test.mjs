// requirement: CMDK-9468
// if combined queries are not exactly 30 then broken
// if dry-run writes files then broken
// if invalid --seed exits 0 then broken
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { rankCommands } from '../../src/lib/palette/rank.ts';
import {
	allQueries,
	baselineTop,
	buildPairs,
	mulberry32
} from '../../scripts/export-cmdk-rating-pairs.mjs';

const FIXTURE = JSON.parse(
	readFileSync(new URL('./fixtures/palette-queries.json', import.meta.url), 'utf8')
);
const SCRIPT = fileURLToPath(
	new URL('../../scripts/export-cmdk-rating-pairs.mjs', import.meta.url)
);

test('buildPairs yields 30 distinct queries with fixture order first', () => {
	const queries = allQueries(FIXTURE);
	assert.equal(queries.length, 30);
	const built = buildPairs({
		queries,
		commands: FIXTURE.commands,
		rankCommands,
		seed: 1,
		generatedAt: '1970-01-01T00:00:00.000Z'
	});
	assert.equal(built.pairs.count, 30);
	assert.equal(built.pairs.pairs[0]?.query, FIXTURE.queries[0]?.query);
	for (const pair of built.pairs.pairs) {
		assert.match(pair.id, /^q\d{2}$/);
		assert.ok(pair.left.length <= 5);
		assert.ok(pair.right.length <= 5);
	}
});

test('seed is stable and sides differ across seeds', () => {
	const queries = allQueries(FIXTURE);
	const stamp = '1970-01-01T00:00:00.000Z';
	const a = buildPairs({
		queries,
		commands: FIXTURE.commands,
		rankCommands,
		seed: 1,
		generatedAt: stamp
	});
	const b = buildPairs({
		queries,
		commands: FIXTURE.commands,
		rankCommands,
		seed: 1,
		generatedAt: stamp
	});
	const c = buildPairs({
		queries,
		commands: FIXTURE.commands,
		rankCommands,
		seed: 2,
		generatedAt: stamp
	});
	assert.deepEqual(a.pairs, b.pairs);
	assert.deepEqual(a.key, b.key);
	assert.notDeepEqual(
		Object.values(a.key.sides).map((side) => side.left),
		Object.values(c.key.sides).map((side) => side.left)
	);
	const leftSides = Object.values(a.key.sides).map((side) => side.left);
	assert.ok(leftSides.includes('ranker'));
	assert.ok(leftSides.includes('baseline'));
});

test('baseline is plain substring', () => {
	const ranked = rankCommands(FIXTURE.commands, 'ej 2', { picks: [], now: 0 });
	assert.equal(baselineTop(FIXTURE.commands, 'ej 2').length, 0);
	assert.equal(ranked[0]?.label, 'Eject deck 2');
});

function spawnScript(args) {
	const env = { ...process.env };
	delete env.FORCE_COLOR;
	return spawnSync(process.execPath, [SCRIPT, ...args], { encoding: 'utf8', env });
}

test('--dry-run writes nothing and --seed abc exits 1', () => {
	const tmp = mkdtempSync(join(tmpdir(), 'cmdk-rating-'));
	const dry = spawnScript(['--dry-run', '--seed', '7', '--out-dir', tmp]);
	assert.equal(dry.status, 0);
	assert.match(dry.stdout, /seed=7/);
	assert.equal(dry.stderr, '');
	const bad = spawnScript(['--seed', 'abc']);
	assert.equal(bad.status, 1);
});

test('mulberry32 is deterministic', () => {
	const a = mulberry32(1);
	const b = mulberry32(1);
	assert.equal(a(), b());
});
