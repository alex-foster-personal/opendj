/**
 * Keep the narrow AutoPlay column on a monochrome inline icon, rather than a
 * platform-coloured emoji glyph that changes appearance across browsers.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const table = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)),
	'utf8'
);

function autoPlayHeader() {
	const start = table.indexOf('class="h-icon h-autoplay"');
	assert.notEqual(start, -1, 'no AutoPlay icon header in TrackTable');
	const open = table.lastIndexOf('<th', start);
	const close = table.indexOf('</th>', start);
	return table.slice(open, close);
}

test('AutoPlay header uses an inline monochrome icon, not an emoji', () => {
	const header = autoPlayHeader();
	assert.match(header, /<AutoPlayExplainer\b[^>]*queue=\{autoPlayQueue\.entries\}/);
	assert.match(header, /<svg\b[^>]*class="autoplay-icon"/);
	assert.match(header, /stroke="currentColor"/);
	assert.equal(header.includes('🤖'), false, 'platform-coloured robot emoji returned');
});
