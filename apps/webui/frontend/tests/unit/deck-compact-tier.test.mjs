/**
 * Deck column compact/narrow tiers by container query (SKINLAYOUT-10203, issue #10203).
 *
 * Regression lines:
 * - if .deck-col is not a named inline-size container deck then broken
 * - if deck tiers use @media or a third breakpoint then broken
 * - if data-priority is not only 2 or 3 then broken
 * - if compact tier scales font-size under 9px then broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const ROOT = fileURLToPath(new URL('../../', import.meta.url));
const read = (rel) => readFileSync(`${ROOT}${rel}`, 'utf8');

const FILES = {
	page: 'src/routes/performance/+page.svelte',
	deck: 'src/lib/components/rb/Deck.svelte',
	header: 'src/lib/components/rb/deck/DeckHeader.svelte',
	hotCue: 'src/lib/components/rb/deck/HotCueBank.svelte'
};

/** First `.deck-col { ... }` rule body in a Svelte/CSS source. */
function deckColBlock(source) {
	const marker = /\.deck-col\s*\{/;
	const match = marker.exec(source);
	assert(match, '.deck-col rule missing');
	let i = source.indexOf('{', match.index);
	assert(i >= 0);
	let depth = 0;
	const start = i + 1;
	for (; i < source.length; i++) {
		const ch = source[i];
		if (ch === '{') depth++;
		else if (ch === '}') {
			depth--;
			if (depth === 0) return source.slice(start, i);
		}
	}
	throw new Error('unclosed .deck-col block');
}

/** Every `@media (...)` condition string in source, in file order. */
function mediaQueries(source) {
	const out = [];
	const re = /@media\s*\(([^)]+)\)/g;
	let m;
	while ((m = re.exec(source)) !== null) out.push(`(${m[1]})`);
	return out;
}

/** Bodies of top-level `@container ... { ... }` blocks (brace-balanced). */
function containerBlockBodies(source) {
	const bodies = [];
	let idx = 0;
	while (idx < source.length) {
		const at = source.indexOf('@container', idx);
		if (at < 0) break;
		const brace = source.indexOf('{', at);
		assert(brace >= 0, '@container without brace');
		let depth = 0;
		const start = brace + 1;
		let i = brace;
		for (; i < source.length; i++) {
			const ch = source[i];
			if (ch === '{') depth++;
			else if (ch === '}') {
				depth--;
				if (depth === 0) {
					bodies.push(source.slice(start, i));
					idx = i + 1;
					break;
				}
			}
		}
	}
	return bodies;
}

const sources = Object.fromEntries(Object.entries(FILES).map(([k, rel]) => [k, read(rel)]));

test('.deck-col declares deck inline-size container', () => {
	const block = deckColBlock(sources.page);
	assert.match(block, /container-type:\s*inline-size/);
	assert.match(block, /container-name:\s*deck/);
});

test('only compact 359px and narrow 299px deck container breakpoints', () => {
	const all = Object.values(sources).join('\n');
	const widths = [];
	const re = /@container deck \(max-width: (\d+)px\)/g;
	let m;
	while ((m = re.exec(all)) !== null) widths.push(Number(m[1]));
	assert.deepEqual(
		[...new Set(widths)].sort((a, b) => b - a),
		[359, 299]
	);
});

test('data-priority marks and values', () => {
	const all = Object.values(sources).join('\n');
	const prios = [...all.matchAll(/data-priority=["'](\d)["']/g)].map((x) => Number(x[1]));
	for (const n of prios) assert.ok(n === 2 || n === 3, `bad priority ${n}`);
	assert.match(sources.header, /class="art-slot" data-priority="3"/);
	assert.match(sources.hotCue, /class="cue-label" data-priority="3"/);
	assert.match(sources.deck, /class="loop-col" data-priority="2"/);
	assert.match(sources.deck, /data-priority="3"[\s\S]*<BeatJump/);
	assert.match(sources.deck, /data-priority="2"[\s\S]*<GridEditButtons/);
});

test('@media baseline unchanged in touched files', () => {
	assert.deepEqual(mediaQueries(sources.page), [
		'(max-height: 799px)',
		'(prefers-reduced-motion: reduce)'
	]);
	assert.deepEqual(mediaQueries(sources.hotCue), ['(prefers-reduced-motion: reduce)']);
	assert.deepEqual(mediaQueries(sources.deck), []);
	assert.deepEqual(mediaQueries(sources.header), []);
});

test('no sub-9px font-size inside @container blocks', () => {
	const all = Object.values(sources).join('\n');
	for (const body of containerBlockBodies(all)) {
		const sizes = [...body.matchAll(/font-size:\s*([\d.]+)px/g)];
		for (const [, px] of sizes) {
			assert.ok(Number(px) >= 9, `font-size ${px}px under 9px floor`);
		}
	}
});

test('hidden parts stay on the performance bus', () => {
	const ipc = read('src/lib/rb/performance-ipc-types.ts');
	for (const cmd of [
		"type: 'beat_jump'",
		"type: 'beat_loop'",
		"type: 'loop'",
		"type: 'grid_edit'",
		"type: 'unload'"
	]) {
		assert.match(ipc, new RegExp(cmd.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
	}
});
