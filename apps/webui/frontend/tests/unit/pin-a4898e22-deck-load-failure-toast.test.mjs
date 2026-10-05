/**
 * pin a4898e22 (remainder): "Selection wouldn't load - see errors. Several
 * weird errors". A failed deck load raised a generic headline ("Deck N could
 * not load the track") with the reason hidden in the expandable detail, and
 * then a SECOND toast for the same failure ("Performance command failed -
 * ..."), because the engine and the command dispatcher each reported it. A
 * load refused before it reached the engine got only the second one, which
 * never said it was a track load at all.
 *
 * - if a known failure code does not become plain words in the headline then
 *   the operator has to expand a toast to learn the file is missing -> broken
 * - if an unknown failure hides its reason behind a generic headline -> broken
 * - if one failed load raises two toasts -> broken
 * - if a load refused before the engine is not reported as a load -> broken
 * - if a stick refusal loses its own wording -> broken (the overshoot)
 * - if the track lookup's bare 404 names the toast differently from the audio
 *   route's TRACK_NOT_FOUND for the same missing track -> broken (a timing race)
 *
 * Synthetic ids and placeholder titles only.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'http://127.0.0.1:65530';
let harness;

before(async () => {
	harness = await loadTypeScriptModule('tests/unit/fixtures/stick-load-failure-toast-entry.ts', {
		viteApiBase: API_BASE
	});
	globalThis.fetch = async () => new Response(null, { status: 204 });
});

beforeEach(() => {
	harness.toasts.length = 0;
});

const CODED = [
	['TRACK_NOT_FOUND', 404, 'Deck 3: this track is no longer in the library'],
	['AUDIO_FILE_MISSING', 404, "Deck 3: this track's audio file is missing on this machine"],
	['CLOUD_ASSET_UNAVAILABLE', 404, "Deck 3: this track's audio is not on this machine and could not be fetched"],
	['CLOUD_POLICY_UNCONFIGURED', 503, 'Deck 3: cloud audio is not set up on this machine, so this track cannot be fetched'],
	['AUDIO_ACCESS_BLOCKED', 503, "Deck 3: this track's file did not open in time - its drive or folder is not answering"]
];

test('a known load failure code names the reason in the headline', () => {
	for (const [code, status, headline] of CODED) {
		const cause = new harness.RbApiError(status, code, 'server detail');
		harness.reportDeckLoadFailure(3, `Placeholder Title: ${cause.message}`, cause, {}, {});
		const toast = harness.toasts.at(-1);
		assert.equal(toast.headline, headline);
		assert.equal(toast.kind, 'error');
		assert.equal(
			toast.message,
			`Deck 3 load failed - Placeholder Title: ${code}: server detail`,
			'the technical text is kept for the detail and the log'
		);
	}
});

test('an undecodable file and an unreachable engine are named in plain words', () => {
	const decode = new Error('Unable to decode audio data');
	decode.name = 'EncodingError';
	assert.equal(
		harness.deckLoadFailureHeadline(1, decode),
		"Deck 1: this track's audio file could not be decoded"
	);
	for (const text of ['Failed to fetch', 'Load failed', 'NetworkError when attempting to fetch resource.']) {
		assert.equal(
			harness.deckLoadFailureHeadline(1, new TypeError(text)),
			'Deck 1: the engine did not answer while loading this track'
		);
	}
});

test('an unrecognized failure still puts its own reason in the headline', () => {
	assert.equal(
		harness.deckLoadFailureHeadline(2, new Error('deck 2 must be fully stopped before replacement')),
		'Deck 2 could not load the track: deck 2 must be fully stopped before replacement'
	);
	assert.equal(
		harness.deckLoadFailureHeadline(2, new harness.RbApiError(500, 'SOMETHING_NEW', 'engine said no')),
		'Deck 2 could not load the track: SOMETHING_NEW: engine said no'
	);
	assert.equal(harness.deckLoadFailureHeadline(2, 'plain string'), 'Deck 2 could not load the track: plain string');
	const long = harness.deckLoadFailureHeadline(2, new Error(`first line ${'x'.repeat(300)}\nsecond line`));
	assert.ok(long.length <= 'Deck 2 could not load the track: '.length + 120, 'a long reason is cut');
	assert.ok(long.endsWith('...'));
	assert.ok(!long.includes('second line'), 'only the first line of a reason belongs in a headline');
});

test('a stick refusal keeps its own headline', () => {
	const cause = new harness.RbApiError(404, 'USB_FILE_MISSING', 'file missing');
	harness.reportDeckLoadFailure(2, `stick-id: ${cause.message}`, cause, {}, {});
	assert.equal(harness.toasts.at(-1).headline, "This track's audio file is missing from the stick");
});

test('one failed load raises one toast: the dispatcher does not repeat the engine', () => {
	const cause = new harness.RbApiError(404, 'AUDIO_FILE_MISSING', 'gone');
	harness.reportDeckLoadFailure(4, `Placeholder Title: ${cause.message}`, cause, {}, {});
	assert.equal(harness.toasts.length, 1);
	harness.reportDeckLoadCommandFailure(4, cause.message, cause);
	assert.equal(harness.toasts.length, 1, 'the same error object must not be toasted twice');
});

test('a load refused before the engine is reported once, as a load, with its reason', () => {
	const cause = new Error('rescue restore owns controls at restoring; command load rejected');
	harness.reportDeckLoadCommandFailure(1, cause.message, cause);
	assert.equal(harness.toasts.length, 1);
	const toast = harness.toasts[0];
	assert.equal(toast.kind, 'error');
	assert.equal(toast.message, `Deck 1 load failed - ${cause.message}`);
	assert.equal(
		toast.headline,
		'Deck 1 could not load the track: rescue restore owns controls at restoring; command load rejected'
	);
});

test('a load whose own toast was suppressed is still reported by the dispatcher', () => {
	const cause = new harness.RbApiError(404, 'AUDIO_FILE_MISSING', 'gone');
	harness.reportDeckLoadFailure(2, cause.message, cause, {}, { suppressFailureToast: true });
	assert.equal(harness.toasts.length, 0, 'control: suppression really suppresses the engine toast');
	harness.reportDeckLoadCommandFailure(2, cause.message, cause);
	assert.equal(harness.toasts.length, 1, 'an untoasted failure must not be mistaken for a toasted one');
});

test('the command dispatcher routes a failed load through the deck-load reporter', () => {
	const src = readFileSync(
		new URL('../../src/lib/rb/performance-ipc.svelte.ts', import.meta.url),
		'utf8'
	);
	const from = src.indexOf('function _persistCommandError(');
	const body = src.slice(from, src.indexOf('\n}\n', from));
	assert.match(
		body,
		/if \(command !== undefined && command\.type === 'load'\) \{\s*reportDeckLoadCommandFailure\(command\.deck, messageText, error\);\s*return;\s*\}/
	);
	assert.ok(
		body.indexOf('suppressCommandErrorToast') < body.indexOf('reportDeckLoadCommandFailure'),
		'a caller that suppresses the command toast must still be honored first'
	);
});

test("the track lookup's bare 404 reads as TRACK_NOT_FOUND, and nothing else is rewritten", () => {
	const response = new Response(null, { status: 404 });
	const lookup = harness.libraryTrackLookupError(new harness.ApiError(404, 'HTTP_404', 'Not Found', response));
	assert.equal(lookup.code, 'TRACK_NOT_FOUND');
	assert.equal(harness.deckLoadFailureHeadline(1, lookup), 'Deck 1: this track is no longer in the library');
	// Controls: a coded 404 (a stick), another status, and a non-ApiError pass through as the same object.
	const stick = new harness.ApiError(404, 'USB_TRACK_NOT_FOUND', 'gone', response);
	assert.equal(harness.libraryTrackLookupError(stick), stick);
	const server = new harness.ApiError(500, 'HTTP_500', 'Internal Server Error', response);
	assert.equal(harness.libraryTrackLookupError(server), server);
	const plain = new Error('Not Found');
	assert.equal(harness.libraryTrackLookupError(plain), plain);
});
