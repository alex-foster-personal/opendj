import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { compile } from 'svelte/compiler';

const filename = new URL('../../src/lib/components/rb/browser/PlaylistTree.svelte', import.meta.url);
const source = readFileSync(filename, 'utf8');

// Issue #881: real counts stay aligned at rest and under selection/hover;
// a screenshot decoration must not imply analysis state that does not exist.
test('playlist rows contain no fabricated EXTRA badge or name-based analysis state', () => {
	assert.doesNotMatch(source, /_hasExtraBadge|badge-extra|CUE Analysis Playlist/);
});

test('All Tracks count hover quotes non-broken tracks, not playable (issue #3883 AC6)', () => {
	assert.match(source, /\$\{allTracksCount\} non-broken tracks, \$\{allTracksBrokenCount\} broken tracks/);
	assert.doesNotMatch(source, /playable tracks/);
});

test('the real non-broken count is the final row item after actions and selection badges', () => {
	// Not anchored to {/each}: a sibling next-step hint (issue #3183) can
	// follow the row inside the loop body without becoming a row child, so
	// this only asserts the count is the LAST element INSIDE the row div.
	assert.match(source, /<span class="count" title=\{_playlistCountTitle\(node\)\}>\{node\.track_count - node\.broken_count\}<\/span>\s*<\/div>/);
	assert.match(
		source,
		/return `\$\{node\.track_count - node\.broken_count\} non-broken tracks, \$\{node\.broken_count\} broken tracks`;/
	);
	assert.match(
		source,
		/<span class="name" title=\{node\.mostly_broken \? formatMostlyBrokenTooltip\(\) : node\.name\}>\{node\.name\}<\/span>/
	);
});

test('resting count cells have a right-aligned minimum column and vertical centering', () => {
	const count = source.match(/\n\t\.count \{([^}]+)\}/)?.[1];
	assert.ok(count, 'count styling must exist outside hover/selected rules');
	for (const declaration of ['flex: none;', 'min-width: 4ch;', 'align-self: stretch;', 'display: flex;', 'align-items: center;', 'justify-content: flex-end;', 'font-variant-numeric: tabular-nums;']) {
		assert.ok(count.includes(declaration), `count alignment requires ${declaration}`);
	}
	assert.match(source, /\.name \{[^}]*min-width: 0;[^}]*overflow: hidden;[^}]*text-overflow: ellipsis;/);
});

test('the production PlaylistTree compiles with its actual bindings and CSS', () => {
	const compiled = compile(source, { filename: filename.pathname, generate: 'client' });
	assert.ok(compiled.js.code.length > 0);
	assert.ok(compiled.css.code.length > 0);
});
