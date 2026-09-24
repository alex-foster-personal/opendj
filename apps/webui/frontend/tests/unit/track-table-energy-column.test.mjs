/**
 * Pin 8e9c273b77c2 (issue #903): Energy column, immediately before Genre.
 * DISPLAY half only -- header is a lightning icon with an accessible/hover
 * explainer, body cell is a single 1-9 digit (dimmed when absent). No value
 * is fabricated: the cell renders whatever `row.energy` already is, and nulls
 * render empty rather than guessing.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

const table = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)),
	'utf8'
);

function energyHeader() {
	const start = table.indexOf('class="h-energy"');
	assert.notEqual(start, -1, 'no Energy header in TrackTable');
	const open = table.lastIndexOf('<th', start);
	const close = table.indexOf('</th>', start);
	return table.slice(open, close);
}

function energyCell() {
	const start = table.indexOf('class="c-energy"');
	assert.notEqual(start, -1, 'no Energy cell in TrackTable');
	const open = table.lastIndexOf('<td', start);
	const close = table.indexOf('</td>', start);
	return table.slice(open, close);
}

test('Energy header is a lightning icon with an accessible hover explainer', () => {
	const header = energyHeader();
	assert.match(header, /title="[^"]*Energy[^"]*1-9[^"]*Mixed In Key[^"]*"/i);
	assert.match(header, /aria-label="[^"]*Energy[^"]*1-9[^"]*Mixed In Key[^"]*"/i);
	// CHROME-01 (issue #3886): the lightning is an SVG icon, never the emoji.
	assert.match(
		header,
		/<svg class="energy-icon" aria-hidden="true"[^>]*><path d="M13 2 3 14h7l-1 8 10-12h-7z"/,
		'header does not carry the lightning icon'
	);
	assert.doesNotMatch(header, /\p{Extended_Pictographic}/u, 'header carries an emoji glyph');
});

test('Energy header sits immediately before the Genre header in column order', () => {
	const energyIdx = table.indexOf('class="h-energy"');
	const genreIdx = table.indexOf("sortableTh('genre'");
	assert.ok(energyIdx !== -1 && genreIdx !== -1 && energyIdx < genreIdx, 'Energy header must precede Genre header');
	// nothing else between them but whitespace/the closing </th>
	const between = table.slice(table.indexOf('</th>', energyIdx), genreIdx);
	assert.doesNotMatch(between, /<th/, 'another header column sits between Energy and Genre');
});

test('Energy cell renders the row value undimmed and dims when absent', () => {
	const cell = energyCell();
	assert.match(cell, /row\.energy/, 'cell does not read row.energy');
	assert.match(cell, /class:energy-unset=\{row\.energy === null\}/, 'no dim state for a missing energy value');
});

test('Energy column has a COL_DEFAULTS width entry and a matching <col>', async () => {
	// COL_DEFAULTS moved to the shared $lib/rb/library-column-widths module
	// (pin batch: compact library display) so every column's default width
	// has one source of truth instead of a copy baked into this component.
	const { COL_DEFAULTS } = await loadTypeScriptModule('src/lib/rb/library-column-widths.ts');
	assert.equal(typeof COL_DEFAULTS.energy, 'number', 'no energy width in COL_DEFAULTS');
	assert.match(table, /<col style=\{`width:\$\{colWidths\.energy\}px`\} \/>/);
});
