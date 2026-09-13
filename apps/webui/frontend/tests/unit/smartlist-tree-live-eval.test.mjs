import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const filename = new URL(
	'../../src/lib/components/rb/browser/TreeSmartlistSection.svelte',
	import.meta.url
);
const source = readFileSync(filename, 'utf8');

test('smartlist rows expose live count, identity, empty state, and their gear', () => {
	assert.match(source, /data-testid="smartlists-folder"/);
	assert.match(source, /data-testid="smartlists-empty"/);
	assert.match(source, /no smartlists yet/);
	assert.match(source, /data-testid="smartlist-row"/);
	assert.match(source, /data-smartlist-id=\{sl\.id\}/);
	assert.match(source, /<svg class="gear"/);
	assert.match(source, /<span class="count">\{sl\.count \?\? '--'\}<\/span>/);
	assert.doesNotMatch(source, /not implemented - see PARITY-TODO/);
});
