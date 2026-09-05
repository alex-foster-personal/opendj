/**
 * Track-table column headers that must stay icons rather than words.
 *
 * SOURCE-SHAPE ON PURPOSE, same reason as capability-gating-markup.test.mjs:
 * this harness is node:test + esbuild with no component mount, so a .svelte
 * file cannot be rendered and queried.
 *
 * Pin e28577797642 (the maintainer, Wed 2 Sep 2026): "artwork col header -> icon.
 * (regression)." The artwork column is 54px of thumbnails; the word "Artwork"
 * does not fit it and the row of narrow columns beside it (funnel, err, cloud)
 * had already settled on the h-icon + inline-svg convention.
 *
 * Regression lines:
 * - if the artwork header renders the word "Artwork" again then it is wider
 *   than its own column for the second time
 * - if it loses the h-icon class then it is no longer centred like its
 *   neighbours and the convention has two members instead of three
 * - if it loses its column-tip explanation then the icon means nothing to a reader
 *   who has not seen the column before
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const table = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)),
	'utf8'
);

/** The `<th ...>...</th>` whose class list contains `h-art`. */
function artHeader() {
	const start = table.indexOf('class="h-icon h-art"');
	assert.notEqual(start, -1, 'no <th class="h-icon h-art"> in TrackTable');
	const open = table.lastIndexOf('<th', start);
	const close = table.indexOf('</th>', start);
	return table.slice(open, close);
}

test('the artwork header is an icon, not the word', () => {
	const th = artHeader();
	assert.match(th, /<svg\b/, 'artwork header has no inline svg');
	assert.equal(
		/>\s*Artwork\s*</.test(th),
		false,
		'the word "Artwork" is back in the header'
	);
});

test('the artwork header keeps its accessible custom column explanation', () => {
	const header = artHeader();
	assert.match(header, /use:columnExplainer=\{\{ text: columnHeaderTitle\('art'\) \}\}/);
	assert.doesNotMatch(header, /\btitle=/, 'the native tooltip competes with the custom explainer');
});

test('it uses the same h-icon convention as its narrow neighbours', () => {
	// funnel / err / cloud / autoplay already sit on h-icon; artwork joins
	// them. Count rather than name them: the point is that h-icon is the
	// shared convention, not that any one column keeps its own modifier.
	const iconHeaders = table.match(/class="h-icon(?: [a-z-]+)?"/g) ?? [];
	assert.ok(iconHeaders.length >= 5, `only ${iconHeaders.length} h-icon headers`);
	assert.ok(table.includes('class="h-icon h-err"'));
	assert.ok(table.includes('class="h-icon h-art"'));
});
