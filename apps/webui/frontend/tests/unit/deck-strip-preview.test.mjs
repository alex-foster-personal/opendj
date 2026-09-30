import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let resolveDeckStripPreview;

before(async () => {
	({ resolveDeckStripPreview } = await loadTypeScriptModule('src/lib/rb/deck-strip-preview.ts'));
});

test('resolveDeckStripPreview reports loading while cache is in flight', () => {
	const res = resolveDeckStripPreview({
		stableId: 't1',
		deckAnlz: null,
		cachedAnlz: null,
		listingPreview: null,
		deckPending: true,
		cacheLoading: false
	});
	assert.equal(res.loading, true);
	assert.equal(res.waveform, null);
});

test('resolveDeckStripPreview prefers listing preview before terminal empty', () => {
	const bands = new Uint8Array(360);
	bands[0] = 40;
	const res = resolveDeckStripPreview({
		stableId: 't1',
		deckAnlz: null,
		cachedAnlz: null,
		listingPreview: { cols: 120, bands, max: 40 },
		deckPending: false,
		cacheLoading: false
	});
	assert.equal(res.source, 'listing');
	assert.notEqual(res.waveform, null);
});
