/** Requirements: PERF-UI-01, LIBUX-13. */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

test('compact utility columns fit real analysis grids and keep room for row numbers', async () => {
	const { compactUtilityWidths } = await loadTypeScriptModule('src/lib/rb/library-column-widths.ts');
	assert.deepEqual(compactUtilityWidths(19), { funnel: 18, err: 20, cloud: 28, order: 24 });
	assert.equal(compactUtilityWidths(8558).order, 32);
	assert.equal(compactUtilityWidths(100000).order, 42);
	assert.equal(compactUtilityWidths(0).order, 24);
	for (const invalid of [-1, 1.5, NaN, Infinity]) {
		assert.throws(() => compactUtilityWidths(invalid), /non-negative safe integer/);
	}
});

test('compact musical columns fit displayed key and BPM text without widening defaults', async () => {
	const { autoMusicalWidths, compactMusicalWidths, COL_DEFAULTS } = await loadTypeScriptModule(
		'src/lib/rb/library-column-widths.ts'
	);
	// Real WebKit measurement: a rendered 10B needs 29.23px and 124 needs 33px
	// after the table cell's existing horizontal padding.
	assert.deepEqual(compactMusicalWidths([]), { key: 30, bpm: 33 });
	assert.deepEqual(compactMusicalWidths([{ key: '8A', bpm: 120 }, { key: '12B', bpm: 128.4 }]), {
		key: 30,
		bpm: 33
	});
	assert.deepEqual(compactMusicalWidths([{ key: 'unparsed-key', bpm: 12345 }]), {
		key: COL_DEFAULTS.key,
		bpm: COL_DEFAULTS.bpm
	});
	assert.deepEqual(
		autoMusicalWidths({ key: 31, bpm: 39 }, { key: 30, bpm: 33 }, new Set(['key'])),
		{ key: 31, bpm: 33 }
	);
});

test('auto-fit follows viewport and row-number changes while preserving manually resized columns', async () => {
	const source = await readFile('src/lib/components/rb/browser/TrackTable.svelte', 'utf8');
	assert.match(source, /const maxRowOrder = \$derived\(rows\.reduce/);
	assert.match(source, /const compact = compactUtilityWidths\(maxRowOrder\);/);
	assert.match(source, /const musical = compactMusicalWidths\(rows\);/);
	assert.match(source, /autoMusicalWidths\(current, musical, manuallyResizedColumns\)/);
	assert.match(source, /manuallyResizedColumns\.add\(col\)/);
	assert.match(source, /void wrapWidth;/);
	assert.match(source, /\.c-funnel,[\s\S]*?\.c-cloud \{\s*padding: 0 2px;/);
	assert.match(source, /\.c-order \{\s*font-size: 9px;\s*padding: 0 2px;/);
});
