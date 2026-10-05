/**
 * Issue #3980: toast clipboard extras (deck/sync/context).
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let gather;

before(async () => {
	const mod = await loadTypeScriptModule('src/lib/toast-copy-context.ts');
	gather = mod.gatherToastCopyExtras;
});

test('FB-19: gatherToastCopyExtras includes ctx_ keys from reportContext', () => {
	const extras = gather({
		pathname: '/',
		reportContext: { source: 'deck-load', deck: '2' }
	});
	assert.equal(extras.ctx_source, 'deck-load');
	assert.equal(extras.ctx_deck, '2');
});

test('gatherToastCopyExtras adds deck lines on /performance when reader supplies decks', () => {
	const extras = gather({
		pathname: '/performance',
		readDecks: () => [
			{
				id: 1,
				stable_id: 'track-stable-abc',
				bpm: 128.4,
				beat_sync_enabled: true,
				sync_mode: 'bar',
				is_master: true
			},
			{
				id: 2,
				stable_id: null,
				bpm: null,
				beat_sync_enabled: false,
				sync_mode: 'beat',
				is_master: false
			}
		]
	});
	assert.equal(extras.master_deck, '1');
	assert.match(extras.deck_1, /stable=track-stable-abc/);
	assert.match(extras.deck_1, /bpm=128/);
	assert.match(extras.deck_1, /sync=bar/);
	assert.match(extras.deck_2, /stable=none/);
});

test('gatherToastCopyExtras omits deck block off /performance', () => {
	const extras = gather({
		pathname: '/',
		readDecks: () => [
			{
				id: 1,
				stable_id: 'x',
				bpm: 120,
				beat_sync_enabled: false,
				sync_mode: 'beat',
				is_master: false
			}
		]
	});
	assert.equal(extras.deck_1, undefined);
});

test('gatherToastCopyExtras includes last perf error on /performance', () => {
	const extras = gather({
		pathname: '/performance',
		readLastError: () => ({ kind: 'toast-error', message: 'boom' })
	});
	assert.equal(extras.last_perf_error, 'toast-error: boom');
});
