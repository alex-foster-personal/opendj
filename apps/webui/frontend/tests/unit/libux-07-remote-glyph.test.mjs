/**
 * // requirement: LIBUX-07
 * Source-shape regression for the library LHS remote-audio glyph.
 *
 * [if] a track's audio is held remotely rather than locally [then] a distinct
 * symbol renders in an LHS status column of that library row
 * [if] the row is streaming or awaiting-volume / missing [then] that symbol
 * is not the streaming cloud and not the missing '!'
 * [if] a track's audio is local [then] no remote symbol renders
 *
 * No jsdom: repo idiom for .svelte assertions is source-level
 * (see suggest-next-strip.test.mjs).
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const TRACK_TABLE = fileURLToPath(
	new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)
);
const source = readFileSync(TRACK_TABLE, 'utf8').replaceAll('\r\n', '\n');
const template = source.slice(source.lastIndexOf('</script>'));
const cloudCell = template.slice(
	template.indexOf('td class="c-cloud"'),
	template.indexOf('td class="c-order"')
);

test('the remote glyph lives in the existing LHS cloud status cell, not a new column', () => {
	assert.match(cloudCell, /row\.is_remote/);
	assert.match(cloudCell, /class="remote"/);
	assert.doesNotMatch(template, /class="c-remote"/);
});

test('the remote glyph is distinct from the streaming cloud and the missing-file mark', () => {
	assert.match(cloudCell, /class="cloud"/);
	assert.match(cloudCell, /class="missing"/);
	assert.match(cloudCell, /class="remote"/);
	assert.notEqual(
		cloudCell.indexOf('class="remote"'),
		cloudCell.indexOf('class="cloud"'),
		'remote and streaming must not share one class'
	);
});

test('the remote glyph carries a title that names the remote-audio state', () => {
	assert.match(cloudCell, /class="remote"[^>]*title="[^"]*remote audio/);
});

test('streaming rows keep the existing cloud and never take the remote class', () => {
	const streamingBlock = cloudCell.slice(0, cloudCell.indexOf('row.is_remote'));
	assert.match(streamingBlock, /is_streaming/);
	assert.doesNotMatch(streamingBlock, /class="remote"/);
});

test('the missing-file mark is the fallback after remote, so a remote row is not painted as broken', () => {
	const remoteAt = cloudCell.indexOf('row.is_remote');
	const missingAt = cloudCell.indexOf('!row.file_exists');
	assert.ok(remoteAt >= 0 && missingAt > remoteAt, 'remote branch must precede the missing-file mark');
});

test('a remote row is not classed broken the way a missing local file is', () => {
	assert.match(source, /row\.is_remote !== true/);
});
