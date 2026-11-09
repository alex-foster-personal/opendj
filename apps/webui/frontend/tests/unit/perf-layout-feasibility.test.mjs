// requirement: SKINLAYOUT-10201
// [if] a performance preset's topbar + waverows + deck row exceed 800 px [then] decks clip at the 1280x800 contract window, [else stop].
// [if] the 900 px deck row fixture passes, or its failure omits the preset and px over [then] the contract test is blind, [else stop].
// [if] presetFitsWindow's reason omits the preset id, window height or needed height [then] a refusal cannot be acted on, [else stop].
// [if] theme.css or the /performance page drifts from the TS px mirror [then] the contract measures the wrong layout, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const themeCss = readFileSync(
	fileURLToPath(new URL('../../src/lib/rb/theme.css', import.meta.url)),
	'utf8'
);
const perfPage = readFileSync(
	fileURLToPath(new URL('../../src/routes/performance/+page.svelte', import.meta.url)),
	'utf8'
);

let L;
let T;
before(async () => {
	L = await loadTypeScriptModule('src/lib/rb/skin-layout.ts');
	T = await loadTypeScriptModule('src/lib/rb/theme-tokens.ts');
});

const grid = () => L.PERFORMANCE_LAYOUT_SPEC['deck-grid'];

test('if theme.css topbar or waverow heights drift from theme-tokens.ts then the contract measures the wrong layout', () => {
	assert.match(themeCss, new RegExp(`--rb-topbar-h:\\s*${T.TOPBAR_H_PX}px`));
	const rows = [...themeCss.matchAll(/--rb-waverow-h:\s*(\d+)px/g)].map((m) => Number(m[1]));
	assert.equal(rows[0], T.WAVEROW_H_PX_DEFAULT);
	assert.equal(Math.max(...rows), T.WAVEROW_H_PX_TALLEST);
});

test('if the /performance page floors or compact rows drift from the deck-grid spec then the contract is stale', () => {
	const compact = new RegExp(
		`@media \\(max-height: ${T.COMPACT_WAVESTACK_BELOW_H_PX - 1}px\\)\\s*\\{[^}]*?` +
			`--rb-waverow-h:\\s*${T.WAVEROW_H_PX_COMPACT}px`
	);
	assert.match(perfPage, compact);
	assert.match(perfPage, new RegExp(`minmax\\(\\s*${grid().floors.deckRowPx}px`));
	assert.match(perfPage, new RegExp(`-\\s*${grid().floors.libraryPx}px`));
	assert.match(
		perfPage,
		new RegExp(`calc\\(${grid().floors.wavestackRows} \\* \\(var\\(--rb-waverow-h\\)`)
	);
});

test('if the .perf-root grid areas differ from the deck-grid spec then the table lies about the page', () => {
	const block = perfPage.match(/\.perf-root \{[\s\S]*?grid-template-areas:([^;]+);/);
	assert.ok(block, '.perf-root grid-template-areas not found');
	const areas = [...block[1].matchAll(/'([^']+)'/g)].map((m) => m[1]);
	assert.deepEqual(areas, [...grid().areas]);
});

test('if the spec table and PERFORMANCE_LAYOUTS disagree, or an id differs from its key, then lookups break', () => {
	assert.deepEqual(
		Object.keys(L.PERFORMANCE_LAYOUT_SPEC).sort(),
		[...L.PERFORMANCE_LAYOUTS].sort()
	);
	for (const [k, spec] of Object.entries(L.PERFORMANCE_LAYOUT_SPEC)) {
		assert.equal(spec.id, k);
	}
});

test('if any shipped preset overflows 1280x800, 2035x1449 or its own minWindow then decks clip', () => {
	for (const id of L.PERFORMANCE_LAYOUTS) {
		const spec = L.PERFORMANCE_LAYOUT_SPEC[id];
		const overflows = L.presetContractOverflows(spec);
		assert.deepEqual(overflows, [], overflows.join('\n'));
		for (const size of [...L.CONTRACT_WINDOWS, spec.minWindow]) {
			const fit = L.presetFitsWindow(spec, size);
			assert.ok(fit.ok, `${id} at ${size.w}x${size.h}: ${fit.reason}`);
		}
	}
});

test('if the deck-grid comfort need is not LIBUX-01 969 px (Default rows) and 1021 px (tallest) then the arithmetic drifted', () => {
	assert.equal(L.presetComfortNeedPx(grid(), T.WAVEROW_H_PX_DEFAULT), 969);
	assert.equal(L.presetComfortNeedPx(grid(), T.WAVEROW_H_PX_TALLEST), 1021);
});

test('if a 900 px deck row fixture passes or its failure omits the preset and px over then the contract is blind', () => {
	const fixture = {
		...grid(),
		id: 'fixture-tall-deck',
		floors: { ...grid().floors, deckRowPx: 900 }
	};
	const overflows = L.presetContractOverflows(fixture);
	const tallDeckMsg =
		'preset fixture-tall-deck at 1280x800: needs 1152 px (28 px topbar + 4 x 56 px waverows + ' +
		'900 px deck row), window is 800 px: 352 px over';
	assert.ok(overflows.includes(tallDeckMsg), overflows.join('\n'));
	assert.ok(overflows.some((m) => /900 px deck row/.test(m) && /352 px over/.test(m)));
});

test('if presetFitsWindow accepts a short or narrow window, or its reason omits id and heights, then refusals are useless', () => {
	const short = L.presetFitsWindow(grid(), { w: 1280, h: 600 });
	assert.equal(short.ok, false);
	for (const part of ['deck-grid', '600 px', '720 px']) {
		assert.ok(short.reason.includes(part), short.reason);
	}
	const narrow = L.presetFitsWindow(grid(), { w: 1000, h: 800 });
	assert.equal(narrow.ok, false);
	assert.match(narrow.reason, /1000 px wide/);
	assert.throws(() => L.presetFitsWindow(grid(), { w: 1280, h: Number.NaN }), /window h/);
});
