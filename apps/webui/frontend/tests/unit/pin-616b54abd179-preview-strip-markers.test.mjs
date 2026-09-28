/**
 * pin 616b54abd179: hot cues and segmentation nodes in the browser mini preview
 * (LIBUX-12). Issue comment Sat 26 Sep 2026: behaviour already on main via
 * drawPointCueMarkers in PreviewStrip.svelte — regression test only.
 *
 * [if] PreviewStrip skips loop bands, phrase markers, or point cues [then STOP]
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const PREVIEW_STRIP = fileURLToPath(
	new URL('../../src/lib/components/rb/browser/PreviewStrip.svelte', import.meta.url)
);
const TRACK_TABLE = fileURLToPath(
	new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)
);

test('pin 616b54abd179: PreviewStrip paints loop bands, phrase segmentation, and hot cues', () => {
	const source = readFileSync(PREVIEW_STRIP, 'utf8');
	const loop = source.indexOf('drawLoopCueBands(');
	const phrase = source.indexOf('drawPhraseMarkers(ctx, markerAnlz.phrases');
	const point = source.indexOf('drawPointCueMarkers(ctx, markerAnlz.cues');

	assert.ok(loop >= 0, 'mini preview must paint stored loop spans');
	assert.ok(phrase >= 0, 'mini preview must paint phrase segmentation boundaries');
	assert.ok(point >= 0, 'mini preview must paint hot/memory point cues');
	assert.ok(loop < phrase, 'loop bands paint behind phrase markers');
	assert.ok(phrase < point, 'point cues stay in the foreground');
	const previewLoopCall = source.slice(loop, phrase);
	assert.match(previewLoopCall, /PREVIEW_MARKER_BAND_PX/);
});

test('pin 616b54abd179: TrackTable passes resolved markerAnlz into each preview row', () => {
	const source = readFileSync(TRACK_TABLE, 'utf8');
	assert.match(source, /markerAnlz=\{markerAnlzById\[row\.stable_id\] \?\? null\}/);
});
