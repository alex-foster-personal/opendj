import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const TRACK_TABLE = fileURLToPath(
	new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)
);

const widths = await loadTypeScriptModule('src/lib/rb/library-column-widths.ts');
const source = readFileSync(TRACK_TABLE, 'utf8').replaceAll('\r\n', '\n');

test('compactOrderWidth fits the largest materialized order without excess width', () => {
	assert.equal(widths.compactOrderWidth(19), 24);
	assert.equal(widths.compactOrderWidth(8558), 32);
	assert.equal(widths.compactOrderWidth(100000), 42);
	assert.equal(widths.compactOrderWidth(0), 24);
});

test('compactOrderWidth rejects values that cannot be rendered as row order', () => {
	for (const value of [-1, 1.5, Number.NaN, Number.POSITIVE_INFINITY]) {
		assert.throws(() => widths.compactOrderWidth(value), /non-negative safe integer/);
	}
});

test('TrackTable changes only the order width and keeps the narrow order column centered', () => {
	assert.match(source, /import \{ compactOrderWidth \} from '\$lib\/rb\/library-column-widths'/);
	assert.match(source, /const maxRowOrder = \$derived\(rows\.reduce\(/);
	assert.match(
		source,
		/colWidths = \{ \.\.\.untrack\(\(\) => colWidths\), order: compactOrderWidth\(maxRowOrder\) \}/,
		'order-width refresh must preserve every user-resized content column'
	);
	assert.match(source, /\.h-order \.th-label \{[\s\S]*?justify-content: center;[\s\S]*?padding: 0 2px;/);
	assert.match(source, /\.c-order \{[\s\S]*?text-align: center;[\s\S]*?font-variant-numeric: tabular-nums;[\s\S]*?padding: 0 2px;/);
});

// Regression: if order width changes any other column or clips the largest row
// number then the pin is broken.
