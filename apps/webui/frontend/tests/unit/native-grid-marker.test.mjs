import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let marker;

before(async () => {
	marker = await loadTypeScriptModule('src/lib/components/rb/deck/strip-native-grid.ts');
});

test('StripWaveform carries the native grid marker text and test id', () => {
	const source = readFrontendSource('src/lib/components/rb/deck/StripWaveform.svelte');
	assert.match(source, /data-testid="native-grid-marker"/);
	assert.match(source, />native grid</);
});

test('[if] deck anlz beatgrid.source is own with real beats [then] shouldShowNativeGridMarker is true, [else stop].', () => {
	const beats = [
		{ n: 1, bpm: 127, t: 0.135 },
		{ n: 2, bpm: 127, t: 0.608 }
	];
	const anlz = {
		beatgrid: { source: 'own', status: 'ok', beats }
	};
	assert.equal(marker.shouldShowNativeGridMarker(anlz), true);
	assert.equal(marker.shouldShowNativeGridMarker({ beatgrid: { source: 'rekordbox', beats } }), false);
	assert.equal(
		marker.shouldShowNativeGridMarker({ beatgrid: { source: 'own', status: 'missing', beats: [] } }),
		false
	);
	assert.equal(
		marker.shouldShowNativeGridMarker({ beatgrid: { source: 'own', status: 'failed', beats: [] } }),
		false
	);
	assert.equal(marker.shouldShowNativeGridMarker({ beatgrid: { source: 'own', status: 'ok', beats: [] } }), false);
});
