// Unit tests for the story-coverage reporter and ratchet (scripts/story-coverage.mjs).
//
// Regression lines:
// - if the report stops printing one line per directory and an overall covered/total then
//   later story issues cannot be measured
// - if --check passes when a fixture tree drops one story then the ratchet cannot fail
// - if a container X.svelte is not covered by XView.stories.ts then the count undercounts
// - if an empty tree or malformed baseline exits 0 then nothing measured reads as a pass

import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { countStoryCoverage } from '../../scripts/story-coverage.mjs';

const SCRIPT = fileURLToPath(new URL('../../scripts/story-coverage.mjs', import.meta.url));
const FRONTEND = fileURLToPath(new URL('../../', import.meta.url));

function makeTree(t, files) {
	const root = mkdtempSync(join(tmpdir(), 'story-coverage-'));
	t.after(() => rmSync(root, { recursive: true, force: true }));
	for (const file of files) {
		mkdirSync(dirname(join(root, file)), { recursive: true });
		writeFileSync(join(root, file), '');
	}
	return root;
}

function run(args) {
	return spawnSync(process.execPath, [SCRIPT, ...args], { encoding: 'utf8' });
}

const TREE = [
	'A.svelte',
	'A.stories.ts',
	'B.svelte',
	'deck/C.svelte',
	'deck/C.stories.ts',
	'deck/D.svelte',
	'deck/D.stories.ts'
];

test('report prints one line per directory and an overall covered/total', (t) => {
	const root = makeTree(t, TREE);
	const result = run(['--root', root]);
	assert.equal(result.status, 0, result.stderr);
	const lines = result.stdout.trim().split('\n');
	const dirs = lines
		.filter((line) => /^\s+(\S+)\s+(\d+)\/(\d+)$/.test(line))
		.map((line) => line.trim().split(/\s+/));
	assert.deepEqual(dirs, [
		['(root)', '1/2'],
		['deck', '2/2']
	]);
	assert.match(lines.at(-1), /^overall 3\/4 \(75\.0%\)$/);
});

test('--check exits 0 at the baseline and 1 when the tree drops one story', (t) => {
	const root = makeTree(t, TREE);
	const base = join(root, 'base.json');
	writeFileSync(base, JSON.stringify({ covered: 3, total: 4 }));
	assert.equal(run(['--root', root, '--check', base]).status, 0);
	rmSync(join(root, 'deck/D.stories.ts'));
	const fell = run(['--root', root, '--check', base]);
	assert.equal(fell.status, 1);
	assert.match(fell.stderr, /\[FAIL\] story coverage fell: 2 covered, baseline 3/);
});

test('XView.stories.ts covers both XView.svelte and its container X.svelte', (t) => {
	const root = makeTree(t, [
		'X.svelte',
		'XView.svelte',
		'XView.stories.ts',
		'Y.svelte',
		'Y.stories.tsx',
		'z.svelte.ts'
	]);
	assert.deepEqual(countStoryCoverage(root), {
		dirs: [{ dir: '(root)', covered: 2, total: 3 }],
		covered: 2,
		total: 3
	});
});

test('an empty tree or a malformed baseline is UNKNOWN (exit 2), not a pass', (t) => {
	const root = makeTree(t, ['README.md']);
	assert.equal(run(['--root', root]).status, 2);
	const tree = makeTree(t, TREE);
	const base = join(tree, 'base.json');
	writeFileSync(base, JSON.stringify({ covered: '3' }));
	assert.equal(run(['--root', tree, '--check', base]).status, 2);
	assert.equal(run(['--bogus', 'x']).status, 2);
});

test('the real tree meets its checked-in baseline', () => {
	const result = spawnSync(
		process.execPath,
		[SCRIPT, '--check', 'budgets/story-coverage/base.json'],
		{ cwd: FRONTEND, encoding: 'utf8' }
	);
	assert.equal(result.status, 0, result.stderr);
});
