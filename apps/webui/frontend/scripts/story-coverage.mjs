#!/usr/bin/env node
// Story coverage: how many Svelte components have a Storybook story, per directory and overall,
// with a ratchet so the covered count never falls.
//
//   node scripts/story-coverage.mjs [--root src/lib/components] [--check budgets/story-coverage/base.json]
//
// Pairing rule, per <dir>/X.svelte: covered iff <dir>/X.stories.ts or <dir>/XView.stories.ts
// exists. So a container X.svelte is covered by its view's XView.stories.ts. `.svelte.ts` rune
// modules are not components; `.stories.tsx` React twins never count.
//
// --check reads {covered, total}; only covered is enforced, total is a record.
// Exit 0 ok, 1 covered below baseline, 2 malformed input or empty tree (UNKNOWN, not a pass).

import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const FRONTEND = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const ROOT_NAME = '(root)';

// ---------------------------------------------------------------- helpers ---

function unknown(message) {
	console.error(`[UNKNOWN] ${message}`);
	process.exit(2);
}

function _parseArgs(argv) {
	const args = { root: 'src/lib/components', check: null };
	for (let i = 0; i < argv.length; i += 2) {
		const flag = argv[i];
		const value = argv[i + 1];
		if (flag !== '--root' && flag !== '--check') unknown(`unknown argument ${flag}`);
		if (value === undefined || value.startsWith('--')) unknown(`${flag} needs a value`);
		args[flag.slice(2)] = value;
	}
	return args;
}

function _isCovered(dir, name) {
	const stem = name.slice(0, -'.svelte'.length);
	return (
		existsSync(join(dir, `${stem}.stories.ts`)) || existsSync(join(dir, `${stem}View.stories.ts`))
	);
}

function _walk(root, dir, rows) {
	const entries = readdirSync(dir, { withFileTypes: true }).sort((a, b) =>
		a.name.localeCompare(b.name)
	);
	let covered = 0;
	let total = 0;
	for (const entry of entries) {
		if (entry.isDirectory()) _walk(root, join(dir, entry.name), rows);
		else if (entry.isFile() && entry.name.endsWith('.svelte')) {
			total += 1;
			if (_isCovered(dir, entry.name)) covered += 1;
		}
	}
	if (total > 0)
		rows.push({ dir: relative(root, dir).split(sep).join('/') || ROOT_NAME, covered, total });
}

function _readBaseline(path) {
	let base;
	try {
		base = JSON.parse(readFileSync(path, 'utf8'));
	} catch (err) {
		unknown(`${path}: ${err.message}`);
	}
	const isCount = (n, min) => Number.isInteger(n) && n >= min;
	if (!base || typeof base !== 'object' || !isCount(base.covered, 0) || !isCount(base.total, 1))
		unknown(`${path}: needs integer covered >= 0 and integer total >= 1`);
	return base;
}

// ------------------------------------------------------------------- core ---

export function countStoryCoverage(root) {
	const rows = [];
	_walk(root, root, rows);
	rows.sort((a, b) =>
		a.dir === ROOT_NAME ? -1 : b.dir === ROOT_NAME ? 1 : a.dir.localeCompare(b.dir)
	);
	const covered = rows.reduce((sum, row) => sum + row.covered, 0);
	const total = rows.reduce((sum, row) => sum + row.total, 0);
	return { dirs: rows, covered, total };
}

function main() {
	const args = _parseArgs(process.argv.slice(2));
	const root = resolve(FRONTEND, args.root);
	if (!existsSync(root) || !statSync(root).isDirectory()) unknown(`${root} is not a directory`);
	const report = countStoryCoverage(root);
	if (report.total === 0) unknown(`${root} holds no .svelte files; nothing was measured`);

	const width = Math.max(...report.dirs.map((row) => row.dir.length));
	console.log(`story coverage (${args.root}, X.stories.ts or XView.stories.ts per X.svelte):`);
	for (const row of report.dirs)
		console.log(`  ${row.dir.padEnd(width)}  ${row.covered}/${row.total}`);
	console.log(
		`overall ${report.covered}/${report.total} (${((report.covered / report.total) * 100).toFixed(1)}%)`
	);

	if (args.check === null) return;
	const file = resolve(FRONTEND, args.check);
	const base = _readBaseline(file);
	if (report.covered < base.covered) {
		console.error(
			`[FAIL] story coverage fell: ${report.covered} covered, baseline ${base.covered} (${args.check}). Add the missing story or restore the deleted one.`
		);
		process.exit(1);
	} else if (report.covered > base.covered) {
		console.log(
			`[OK] story coverage ${report.covered} is above baseline ${base.covered}; raise covered in ${args.check} to ${report.covered} to lock it in.`
		);
	} else {
		console.log(`[OK] story coverage ${report.covered} meets baseline ${base.covered}`);
	}
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
