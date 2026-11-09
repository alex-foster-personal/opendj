/**
 * Vocal-analysis refusal copy (issue #9653, UX-R2-9920): the operator reads
 * words, never a server category, and "already analyzed" counts as done.
 *
 * Regression lines:
 *  - if vocalsRefusalCopy returns a snake_case token then the toast leaks an internal category
 *  - if cached_demucs is an error toast or skips refresh then existing vocals look like a failure
 *  - if an unknown category is not logged with console.error then server drift goes unseen
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
