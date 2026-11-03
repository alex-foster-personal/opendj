import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { compile } from 'svelte/compiler';

// PlaylistTree renders its props-only view, which holds the rows and their CSS.
const filename = new URL('../../src/lib/components/rb/browser/PlaylistTreeView.svelte', import.meta.url);
const source = readFileSync(filename, 'utf8');
const containerSource = readFileSync(
	new URL('../../src/lib/components/rb/browser/PlaylistTree.svelte', import.meta.url),
	'utf8'
);

// Issue #881: real counts stay aligned at rest and under selection/hover;
// a screenshot decoration must not imply analysis state that does not exist.
test('playlist rows contain no fabricated EXTRA badge or name-based analysis state', () => {
	assert.doesNotMatch(source + containerSource, /_hasExtraBadge|badge-extra|CUE Analysis Playlist/);
});

// REQ: LIBM-105 (issue #3883 AC6), changed by LIBM-184 (issue #3934): the All
// Tracks badge and title come from allTracksBadge (all-tracks-badge.test.mjs),
// which counts audio on this machine like the playlist badges below it.
test('All Tracks count and hover come from allTracksBadge, never "playable tracks"', () => {
	assert.match(source, /allTracksBadge\(allTracksCount, allTracksBrokenCount, allTracksAvailability\)/);
	assert.match(
		source,
		/\{allTracksError === null && allBadge\.error === null \? allBadge\.count \?\? '\.\.\.' : '!'\}/
	);
	// The availability prop has no default: an unwired caller fails the type check.
	assert.match(source, /^\t\tallTracksAvailability,$/m);
	assert.doesNotMatch(source, /allTracksAvailability = /);
	const props = readFileSync(new URL('../../src/lib/components/rb/browser/playlist-tree-props.ts', import.meta.url), 'utf8');
	assert.match(props, /^\tallTracksAvailability: unknown;$/m, 'the prop is required, not optional');
	assert.match(source, /return allBadge\.title;/);
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
		/<span class="name" title=\{node\.mostly_broken \? mostlyBrokenTitle : node\.name\}>\{node\.name\}<\/span>/
	);
	assert.match(containerSource, /mostlyBrokenTitle=\{formatMostlyBrokenTooltip\(\)\}/);
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
