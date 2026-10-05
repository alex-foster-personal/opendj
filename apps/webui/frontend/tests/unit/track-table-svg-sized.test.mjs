// Every inline <svg class="..."> in TrackTable carries an explicit width rule.
// An SVG with only a viewBox stretches to fill a flex parent: the plays header
// glyph blew up to the column width once its label was centred (Mon 5 Oct 2026).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const source = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)),
	'utf8'
);
const style = source.slice(source.indexOf('<style'));
const markup = source.slice(0, source.indexOf('<style'));

function hasSizedRule(cls) {
	const rule = new RegExp(`\\.${cls}[^{]*\\{([^}]*)\\}`, 'g');
	for (const m of style.matchAll(rule)) if (/(^|[\s;])width\s*:/.test(m[1])) return true;
	return false;
}

test('every classed inline svg in TrackTable has an explicit width', () => {
	const classes = [...markup.matchAll(/<svg\s+class="([\w-]+)"/g)].map((m) => m[1]);
	assert.ok(classes.includes('plays-icon'), 'control: the plays header glyph is found');
	const unsized = [...new Set(classes)].filter((c) => !hasSizedRule(c));
	assert.deepEqual(unsized, [], `unsized svg classes stretch to their flex parent: ${unsized.join(', ')}`);
});
