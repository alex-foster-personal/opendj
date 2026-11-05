/** Requirements: PERF-UI-01, LIBUX-13, CHROME-20. */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

test('compact utility columns fit real analysis grids and keep room for row numbers', async () => {
	const { compactUtilityWidths } = await loadTypeScriptModule(
		'src/lib/rb/library-column-widths.ts'
	);
	assert.deepEqual(compactUtilityWidths(19), { funnel: 18, err: 20, cloud: 28, order: 24 });
	assert.equal(compactUtilityWidths(8558).order, 32);
	assert.equal(compactUtilityWidths(100000).order, 42);
	assert.equal(compactUtilityWidths(0).order, 24);
	for (const invalid of [-1, 1.5, NaN, Infinity]) {
		assert.throws(() => compactUtilityWidths(invalid), /non-negative safe integer/);
	}
});

test('a manual K/B drag stays authoritative over the measured fit', async () => {
	const { autoMusicalWidths } = await loadTypeScriptModule('src/lib/rb/library-column-widths.ts');
	assert.deepEqual(
		autoMusicalWidths({ key: 31, bpm: 39 }, { key: 70, bpm: 46 }, new Set(['key'])),
		{ key: 31, bpm: 46 }
	);
});

test('auto-fit follows viewport and row-number changes while preserving manually resized columns', async () => {
	const source = await readFile('src/lib/components/rb/browser/TrackTable.svelte', 'utf8');
	assert.match(source, /const maxRowOrder = \$derived\(rows\.reduce/);
	assert.match(source, /const compact = compactUtilityWidths\(maxRowOrder\);/);
	assert.match(source, /const musical = wrapEl === null \? null : fitMusicalColumns\(\{/);
	assert.match(
		source,
		/musical === null \? \{\} : autoMusicalWidths\(current, musical, manuallyResizedColumns\)/
	);
	assert.match(source, /manuallyResizedColumns\.add\(col\)/);
	assert.match(source, /void wrapWidth;/);
	assert.match(source, /\.c-funnel,[\s\S]*?\.c-cloud \{\s*padding: 0 2px;/);
	assert.match(source, /\.c-order \{\s*font-size: 9px;\s*padding: 0 2px;/);
});
