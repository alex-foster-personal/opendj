/**
 * pin be1b8f94f167: horizontal health dots bottom-right with library, lyrics,
 * and stems coverage plus shared labeled hover (#2076 supersedes per-dot popovers).
 *
 * [if] dots float outside the bottom tray or lyricsCompletion is missing [then STOP]
 * [if] hover does not reveal one shared popover listing every check [then STOP]
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const PANEL = fileURLToPath(
	new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)
);

function source() {
	return readFileSync(PANEL, 'utf8');
}

test('pin be1b8f94f167: library health cluster is horizontal in the bottom tray with shared hover detail', () => {
	const src = source();
	const template = src.slice(0, src.indexOf('<style>'));
	assert.match(
		src,
		/<div class="bottom-bar">[\s\S]*class="library-health"/,
		'.library-health must live inside .bottom-bar (bottom-right tray)'
	);
	const libraryHealthCss = src.slice(
		src.indexOf('.library-health {'),
		src.indexOf('.health-dot {')
	);
	assert.match(libraryHealthCss, /display:\s*flex/, 'dots must lay out horizontally');
	assert.equal(
		(template.match(/class="health-popover"/g) ?? []).length,
		1,
		'one shared popover lists every dot with labels'
	);
	assert.match(
		src,
		/\.library-health:hover \.health-popover/,
		'hovering the cluster reveals full detail for all dots'
	);
	assert.doesNotMatch(
		src,
		/\.health-dot:hover \.health-popover/,
		'per-dot popovers must stay gone'
	);
	const match = src.match(/\{#each \[([^\]]+)\] as dot \(dot\.label\)\}/);
	assert.ok(match, 'dot cluster #each block must exist');
	const rendered = match[1].split(',').map((s) => s.trim());
	assert.ok(rendered.includes('libraryHealth'), 'library health dot');
	assert.ok(rendered.includes('lyricsCompletion'), 'lyric analysis completion dot');
	assert.ok(rendered.includes('stemsCompletion'), 'stem analysis completion dot');
});
