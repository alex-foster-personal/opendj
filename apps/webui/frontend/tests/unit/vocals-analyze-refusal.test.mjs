/**
 * Vocal-analysis refusal copy (issue #9653, UX-R2-9920): the operator reads
 * words, never a server category, and "already analyzed" counts as done.
 *
 * Regression lines:
 *  - if vocalsRefusalCopy returns a snake_case token then the toast leaks an internal category
 *  - if cached_demucs is an error toast or skips refresh then existing vocals look like a failure
 *  - if an unknown category is not logged with console.error then server drift goes unseen
 *  - if status preflight (#13329) maps regions to completed toast while missing_file stays error
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SNAKE = /[a-z0-9]+_[a-z0-9]+/;

const EXPECTED = {
	cached_demucs: 'Vocals already analyzed',
	missing_file: 'Audio file not found',
	missing_analysis: 'Track needs analysing first',
	pvdi: 'Vocals already come from rekordbox',
	broken_vocal_cache: 'Saved vocal data is unreadable',
	unknown_track: 'Track not found in the library',
	missing_stems: 'Stem bundle not found',
	in_progress: 'Vocal analysis is already running'
};

let mod;
before(async () => {
	mod = await loadTypeScriptModule('tests/unit/fixtures/vocals-analyze-entry.ts', {
		viteApiBase: 'https://vocals.example.test'
	});
});

test('every server category maps to exact operator words with no snake_case', (t) => {
	const err = t.mock.method(console, 'error', () => {});
	for (const [category, copy] of Object.entries(EXPECTED)) {
		assert.equal(mod.vocalsRefusalCopy(category), copy);
		assert.doesNotMatch(copy, SNAKE);
	}
	assert.equal(err.mock.callCount(), 0);
});

test('unknown or missing category returns the generic sentence and logs the raw value', (t) => {
	const err = t.mock.method(console, 'error', () => {});
	assert.equal(mod.vocalsRefusalCopy('bogus_thing'), mod.VOCALS_REFUSAL_GENERIC);
	assert.equal(err.mock.callCount(), 1);
	assert.ok(err.mock.calls[0].arguments.includes('bogus_thing'));
	assert.equal(mod.vocalsRefusalCopy(undefined), mod.VOCALS_REFUSAL_GENERIC);
	assert.equal(mod.vocalsRefusalCopy('toString'), mod.VOCALS_REFUSAL_GENERIC);
	assert.equal(err.mock.callCount(), 3);
	assert.doesNotMatch(mod.VOCALS_REFUSAL_GENERIC, SNAKE);
});

test('claimed track: info toast, refresh, no category', () => {
	assert.deepEqual(mod.vocalsAnalyzeOutcome('a', { claimed: ['a'], refused: {} }), {
		message: 'Vocal analysis done',
		toastKind: 'info',
		refresh: true,
		category: null
	});
});

test('cached_demucs: info toast, refresh, raw category kept for agents', () => {
	assert.deepEqual(
		mod.vocalsAnalyzeOutcome('a', { claimed: [], refused: { a: 'cached_demucs' } }),
		{
			message: 'Vocals already analyzed',
			toastKind: 'info',
			refresh: true,
			category: 'cached_demucs'
		}
	);
});

test('other refusal: error toast, no refresh, raw category kept', () => {
	assert.deepEqual(mod.vocalsAnalyzeOutcome('a', { claimed: [], refused: { a: 'missing_file' } }), {
		message: 'Audio file not found',
		toastKind: 'error',
		refresh: false,
		category: 'missing_file'
	});
});

test('neither claimed nor refused: generic error, logged', (t) => {
	const err = t.mock.method(console, 'error', () => {});
	const out = mod.vocalsAnalyzeOutcome('a', { claimed: [], refused: {} });
	assert.equal(out.message, mod.VOCALS_REFUSAL_GENERIC);
	assert.equal(out.toastKind, 'error');
	assert.equal(out.refresh, false);
	assert.equal(out.category, null);
	assert.equal(err.mock.callCount(), 1);
});

test('vocalsStatusHasRegions is true only when regions are non-empty', () => {
	assert.equal(
		mod.vocalsStatusHasRegions({
			status: 'demucs',
			fps: 10,
			regions: [{ start_s: 0, end_s: 1, intensity: 1 }]
		}),
		true
	);
	assert.equal(mod.vocalsStatusHasRegions({ status: 'not_analyzed' }), false);
	assert.equal(mod.vocalsStatusHasRegions({ status: 'no_vocals', fps: 10, regions: [] }), false);
});

test('status with regions yields completed outcome, unlike genuine refusal (#13329)', () => {
	const vocals = {
		status: 'demucs',
		fps: 10,
		regions: [{ start_s: 0, end_s: 1, intensity: 1 }]
	};
	const result = mod.vocalsAnalyzeResultFromVocalStatus('track-a', vocals);
	assert.ok(result);
	assert.equal(result.refused['track-a'], 'cached_demucs');
	const completed = mod.vocalsAnalyzeOutcome('track-a', result);
	assert.equal(completed.toastKind, 'info');
	assert.equal(completed.refresh, true);
	assert.equal(completed.message, 'Vocals already analyzed');
	assert.equal(completed.category, 'cached_demucs');
	assert.doesNotMatch(completed.message, SNAKE);
	const refused = mod.vocalsAnalyzeOutcome('track-a', {
		claimed: [],
		refused: { 'track-a': 'missing_file' }
	});
	assert.equal(refused.toastKind, 'error');
	assert.equal(refused.refresh, false);
	assert.equal(refused.message, 'Audio file not found');
	assert.notEqual(completed.toastKind, refused.toastKind);
	assert.notEqual(completed.refresh, refused.refresh);
	assert.equal(mod.vocalsAnalyzeResultFromVocalStatus('track-a', { status: 'not_analyzed' }), null);
});

test('requestTracksRefresh reaches subscribers until unsubscribed; a throwing one does not block others', (t) => {
	t.mock.method(console, 'error', () => {});
	const seen = [];
	const offThrow = mod.subscribeTracksRefreshRequest(() => {
		throw new Error('boom');
	});
	const off = mod.subscribeTracksRefreshRequest((ids) => seen.push(ids));
	mod.requestTracksRefresh(['a']);
	assert.deepEqual(seen, [['a']]);
	off();
	offThrow();
	mod.requestTracksRefresh(['b']);
	assert.deepEqual(seen, [['a']]);
});
