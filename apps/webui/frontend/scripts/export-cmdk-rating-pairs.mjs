// export-cmdk-rating-pairs.mjs -- anonymous ranker-vs-substring pairs for blind rating (issue #9468).
//
// WHY THIS EXISTS
// The Cmd K ranker must beat a plain substring baseline. This script writes 30
// queries' top-5 lists from each side as anonymous left/right pairs, with side
// order from a seeded PRNG and a separate key for scoring.
//
// USAGE
//   node scripts/export-cmdk-rating-pairs.mjs
//   node scripts/export-cmdk-rating-pairs.mjs --seed 7 --out-dir test-results/cmdk-rating
//   node scripts/export-cmdk-rating-pairs.mjs --dry-run
//
// Playwright clears test-results/ on a run; copy the files elsewhere before a rating session.

import { webcrypto } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('..', import.meta.url));
const FIXTURE_PATH = resolve(FRONTEND_ROOT, 'tests/unit/fixtures/palette-queries.json');
const RANK_MODULE = '/src/lib/palette/rank.ts';
const DEFAULT_OUT = resolve(FRONTEND_ROOT, 'test-results/cmdk-rating');
const crypto = webcrypto;

export const RATING_ONLY_QUERIES = [
	'xf',
	'tmp rst',
	'hc a',
	'nxt',
	'dark',
	'stems',
	'anlz',
	'lp 16',
	'q',
	'wave'
];

function _viteServer() {
	return createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true, watch: null },
		plugins: [svelte()],
		resolve: {
			alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') },
			conditions: ['browser']
		}
	});
}

export async function loadRanker() {
	const vite = await _viteServer();
	try {
		const mod = await vite.ssrLoadModule(RANK_MODULE);
		return mod;
	} finally {
		await vite.close();
	}
}

/** Plain substring, the floor the ranker must beat. */
export function baselineTop(commands, q, n = 5) {
	const needle = q.trim().toLowerCase();
	if (needle === '') return [];
	const out = [];
	for (const cmd of commands) {
		const hay = `${cmd.label} ${(cmd.keywords ?? []).join(' ')}`.toLowerCase();
		if (hay.includes(needle)) out.push(cmd);
		if (out.length >= n) break;
	}
	return out.slice(0, n);
}

export function mulberry32(seed) {
	let s = seed >>> 0;
	return () => {
		s += 0x6d2b79f5;
		let t = s;
		t = Math.imul(t ^ (t >>> 15), t | 1);
		t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
		return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
	};
}

export function buildPairs({
	queries,
	commands,
	rankCommands,
	seed,
	generatedAt = new Date().toISOString()
}) {
	const rng = mulberry32(seed);
	const sides = {};
	const pairs = [];

	for (let i = 0; i < queries.length; i++) {
		const query = queries[i];
		const id = `q${String(i + 1).padStart(2, '0')}`;
		const ranked = rankCommands(commands, query, { picks: [], now: 0 }).slice(0, 5);
		const base = baselineTop(commands, query, 5);
		const rankerLeft = rng() < 0.5;
		const left = rankerLeft ? ranked : base;
		const right = rankerLeft ? base : ranked;
		const leftLabels = left.map((row) => row.label);
		const rightLabels = right.map((row) => row.label);
		const identical = JSON.stringify(leftLabels) === JSON.stringify(rightLabels);
		pairs.push({ id, query, left: leftLabels, right: rightLabels, identical });
		sides[id] = {
			left: rankerLeft ? 'ranker' : 'baseline',
			right: rankerLeft ? 'baseline' : 'ranker'
		};
	}

	return {
		pairs: {
			schema: 1,
			seed,
			generated_at: generatedAt,
			universe: 'tests/unit/fixtures/palette-queries.json',
			count: pairs.length,
			pairs
		},
		key: {
			schema: 1,
			seed,
			sides
		}
	};
}

function loadFixtureCommands() {
	const fixture = JSON.parse(readFileSync(FIXTURE_PATH, 'utf8'));
	return fixture.commands;
}

export function allQueries(fixture) {
	const fromFixture = fixture.queries.map((row) => row.query.trim());
	const combined = [...fromFixture, ...RATING_ONLY_QUERIES.map((q) => q.trim())];
	const distinct = [...new Set(combined)];
	if (distinct.length !== 30) {
		throw new Error(`expected 30 distinct queries, got ${distinct.length}`);
	}
	return distinct;
}

function parseSeed(argv) {
	const idx = argv.indexOf('--seed');
	if (idx === -1) return null;
	const raw = argv[idx + 1];
	const n = Number(raw);
	if (!Number.isInteger(n) || n < 0 || n > 2 ** 32 - 1) {
		console.error(`[cmdk-rating] invalid --seed ${raw}; want integer in [0, 2^32)`);
		process.exit(1);
	}
	return n >>> 0;
}

async function main() {
	const argv = process.argv.slice(2);
	const dryRun = argv.includes('--dry-run');
	const outIdx = argv.indexOf('--out-dir');
	const outDir = outIdx === -1 ? DEFAULT_OUT : resolve(argv[outIdx + 1] ?? DEFAULT_OUT);
	let seed = parseSeed(argv);
	if (seed === null) {
		const buf = new Uint32Array(1);
		crypto.getRandomValues(buf);
		seed = buf[0] >>> 0;
	}

	const fixture = JSON.parse(readFileSync(FIXTURE_PATH, 'utf8'));
	const queries = allQueries(fixture);
	const commands = fixture.commands;

	const { rankCommands } = await loadRanker();
	if (typeof rankCommands !== 'function') {
		console.error('[cmdk-rating] rankCommands failed to load');
		process.exit(1);
	}

	const built = buildPairs({ queries, commands, rankCommands, seed });

	for (const pair of built.pairs.pairs) {
		if (pair.left.length > 5 || pair.right.length > 5) {
			console.error(`[cmdk-rating] pair ${pair.id} has more than 5 rows on a side`);
			process.exit(1);
		}
	}
	if (built.pairs.count !== 30) {
		console.error(`[cmdk-rating] expected 30 pairs, got ${built.pairs.count}`);
		process.exit(1);
	}

	const rankerLeftCount = Object.values(built.key.sides).filter(
		(side) => side.left === 'ranker'
	).length;

	if (dryRun) {
		console.log(
			`[cmdk-rating] DRY RUN 30 pairs seed=${seed} ranker-left=${rankerLeftCount}/30 -> ${outDir} (nothing written)`
		);
		return;
	}

	mkdirSync(outDir, { recursive: true });
	writeFileSync(
		resolve(outDir, 'pairs.json'),
		`${JSON.stringify(built.pairs, null, '\t')}\n`,
		'utf8'
	);
	writeFileSync(resolve(outDir, 'key.json'), `${JSON.stringify(built.key, null, '\t')}\n`, 'utf8');
	console.log(`[cmdk-rating] 30 pairs seed=${seed} ranker-left=${rankerLeftCount}/30 -> ${outDir}`);
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
	await main();
}
