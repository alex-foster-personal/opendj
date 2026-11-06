import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// DECKUX-LABEL-02: only real faults stay sticky on the deck banner.
let policy;
let presentation;
before(async () => {
	policy = await loadTypeScriptModule('src/lib/rb/deck-banner-policy.ts');
	presentation = await loadTypeScriptModule('src/lib/toast-presentation.ts');
});

// The exact string the live banner carried on silver, Tue 3 Nov 2026.
const STEMS_LOADING = 'Error: deck 1 stems are loading: no aligned artifact';

test('[if] a stem control hit a deck whose stems are still loading [then] the banner is expected and clears after the toast default, [else stop].', () => {
	assert.equal(policy.deckBannerTone(STEMS_LOADING), 'expected');
	assert.equal(policy.deckBannerDismissMs(STEMS_LOADING, 'loading'), policy.DECK_EXPECTED_BANNER_MS);
	assert.equal(policy.DECK_EXPECTED_BANNER_MS, 5000);
});

test('[if] the stems finished loading after the banner was raised [then] the stale banner clears at once, [else stop].', () => {
	assert.equal(policy.deckBannerDismissMs(STEMS_LOADING, 'ready'), 0);
	assert.equal(policy.deckBannerDismissMs(STEMS_LOADING, 'unavailable'), 0);
});

test('[if] the banner is a real fault [then] it stays until dismissed, [else stop].', () => {
	for (const fault of [
		'Error: deck 2 stems are error: decode failed',
		'Deck 3 processor failed - worklet crashed',
		'Deck 1 load failed - file missing'
	]) {
		assert.equal(policy.deckBannerTone(fault), 'fault', fault);
		assert.equal(policy.deckBannerDismissMs(fault, 'loading'), null, fault);
	}
});

test('[if] the stems-loading text is shown [then] the headline is plain words, not "Deck audio: Error: ...", [else stop].', () => {
	const shown = presentation.formatToastPresentation({ kind: 'error', message: STEMS_LOADING });
	assert.equal(shown.headline, 'Deck 1 stems are still loading');
	assert.equal(shown.detail, STEMS_LOADING);
});

test('isExpectedStemsLoadingMessage classifies session-restore inner text and excludes other failures', () => {
	assert.equal(policy.isExpectedStemsLoadingMessage('deck 2 stems are loading: no aligned artifact'), true);
	assert.equal(policy.isExpectedStemsLoadingMessage('Error: deck 1 stems are loading: no aligned artifact'), true);
	assert.equal(policy.isExpectedStemsLoadingMessage('deck 2 stem restore failed: decode failed'), false);
	assert.equal(policy.isExpectedStemsLoadingMessage('session restore deck 2 failed: boom'), false);
});
