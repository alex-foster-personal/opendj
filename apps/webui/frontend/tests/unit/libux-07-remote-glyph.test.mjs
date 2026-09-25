/**
 * Requirements: LIBUX-07, LIBUX-13.
 * Source-shape regression for the library LHS remote-audio glyph.
 *
 * [if] a track's audio is held remotely rather than locally [then] a distinct
 * symbol renders in an LHS status column of that library row
 * [if] the row is streaming or awaiting-volume / missing [then] that symbol
 * is not the streaming cloud and not the missing '!'
 * [if] a track is local-only [then] LIBUX-13 supersedes the quiet cell with
 * a crossed-out cloud while preserving remote-versus-streaming semantics
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
const TRACK_CLOUD_STATE = fileURLToPath(
	new URL('../../src/lib/components/rb/browser/track-cloud-state.ts', import.meta.url)
);
const source = readFileSync(TRACK_TABLE, 'utf8').replaceAll('\r\n', '\n');
const stateSource = readFileSync(TRACK_CLOUD_STATE, 'utf8').replaceAll('\r\n', '\n');
const template = source.slice(source.lastIndexOf('</script>'));
const cloudCell = template.slice(
	template.indexOf('td class="c-cloud"'),
	template.indexOf('td class="c-order"')
);

// 00a092243 hoisted `{@const cloudView = trackCloudView(...)}` out of the
// <td> (Svelte only allows {@const} as a direct child of a block) to the top of
// the row's {#each}; the cell renders from that per-row view.
const rowCloudView = template.slice(
	template.indexOf('{@const cloudView = trackCloudView({'),
	template.indexOf('<tr', template.indexOf('{@const cloudView = trackCloudView({'))
);

test('the cloud-presence glyph lives in the existing LHS status cell, not a new column', () => {
	assert.match(rowCloudView, /row\.has_remote_copy/);
	assert.match(cloudCell, /cloudView\./);
	assert.match(cloudCell, /<CloudStatusIcon\b/);
	assert.doesNotMatch(template, /class="c-remote"/);
});

test('cloud-presence states stay distinct from streaming', () => {
	const iconSource = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/browser/CloudStatusIcon.svelte', import.meta.url)),
		'utf8'
	);
	assert.match(iconSource, /class:streaming=/);
	assert.match(iconSource, /class:not-on-cloud=/);
});

test('every CloudSync glyph carries the production helper title', () => {
	assert.match(cloudCell, /<CloudStatusIcon[^>]*view=\{cloudView\}/);
	assert.match(stateSource, /Not on CloudSync/);
	assert.match(stateSource, /On CloudSync but not stored locally/);
	assert.match(stateSource, /On CloudSync and stored locally/);
});

test('streaming rows take precedence over CloudSync storage state', () => {
	const streamingAt = stateSource.indexOf('if (input.isStreaming)');
	const remoteAt = stateSource.indexOf('if (input.hasRemoteCopy');
	assert.ok(streamingAt >= 0 && remoteAt > streamingAt);
});

test('a remote copy takes precedence over an absent local file, so cloud-only is red', () => {
	const remoteAt = stateSource.indexOf('if (input.hasRemoteCopy)');
	const localFallbackAt = stateSource.lastIndexOf("'not-on-cloud',");
	assert.ok(remoteAt >= 0 && localFallbackAt > remoteAt, 'remote branch must precede the local fallback');
});

test('a remote row is not classed broken the way a missing local file is', () => {
	assert.match(source, /row\.is_remote !== true/);
});

test('local-only uses a crossed-out cloud instead of the old blank cell', () => {
	const iconSource = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/browser/CloudStatusIcon.svelte', import.meta.url)),
		'utf8'
	);
	assert.match(iconSource, /class:not-on-cloud=/);
	assert.match(iconSource, /d="M3 13 13 3"/);
	assert.match(iconSource, /tick-blue/);
});
