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
	// A width attribute on the tag or a CSS width rule on the class both count.
	const tags = [...markup.matchAll(/<svg\b([^>]*)>/g)].map((m) => m[1]);
	const classed = tags
		.map((attrs) => ({ cls: /class="([\w-]+)"/.exec(attrs)?.[1], attrs }))
		.filter((t) => t.cls);
	assert.ok(classed.some((t) => t.cls === 'plays-icon'), 'control: the plays header glyph is found');
	assert.ok(classed.some((t) => t.cls === 'autoplay-icon'), 'control: an attribute-sized svg is found');
	const unsized = [
		...new Set(classed.filter((t) => !/\swidth="/.test(t.attrs) && !hasSizedRule(t.cls)).map((t) => t.cls))
	];
	assert.deepEqual(unsized, [], `unsized svg classes stretch to their flex parent: ${unsized.join(', ')}`);
});
