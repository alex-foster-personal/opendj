/**
 * @pytest.mark.requirement UX-TOAST-02
 * [if] RangeError in message [then] headline is not "RangeError" [else stop].
 * [if] error toast [then] solutionHint mentions placeholder URL [else stop].
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let presentation;

before(async () => {
	presentation = await loadTypeScriptModule('src/lib/toast-presentation.ts');
});

test('RangeError maps to a human headline, not the exception class', () => {
	const result = presentation.formatToastPresentation({
		kind: 'warn',
		message: 'RangeError',
		feature: 'Beat Sync'
	});
	assert.notEqual(result.headline, 'RangeError');
	assert.match(result.headline, /Beat Sync/);
	assert.match(result.detail ?? '', /RangeError/);
});

test('a plain Error cause keeps its message as the headline; a bare built-in or bare text does not', () => {
	const message = 'Deck 1: no track loaded';
	const plain = presentation.formatToastPresentation({ kind: 'error', message, cause: new Error(message) });
	assert.equal(plain.headline, message);
	const builtIn = presentation.formatToastPresentation({ kind: 'error', message, cause: new RangeError(message) });
	assert.match(builtIn.headline, /hit a limit/);
	const bare = presentation.formatToastPresentation({ kind: 'error', message: 'Error', cause: new Error('Error') });
	assert.match(bare.headline, /hit a limit/);
});

test("a stringified plain Error drops its 'Error: ' from the headline and keeps it in rawMessage", () => {
	const cause = new Error('play: no track loaded on deck 1');
	const message = `Performance command failed - ${cause.name}: ${cause.message}`;
	const result = presentation.formatToastPresentation({ kind: 'error', message, cause });
	assert.equal(result.headline, 'Performance command failed - play: no track loaded on deck 1');
	assert.equal(result.rawMessage, message);
	const typed = new TypeError('bad deck');
	const kept = presentation.formatToastPresentation({ kind: 'warn', message: `TypeError: ${typed.message}`, cause: typed });
	assert.doesNotMatch(kept.headline, /^bad deck$/, 'only a plain Error loses its name');
});

test('error toasts carry a placeholder solutions URL', () => {
	const result = presentation.formatToastPresentation({
		kind: 'error',
		message: 'Deck 2 could not load the track'
	});
	assert.ok((result.solutionHint ?? '').includes(presentation.TOAST_SOLUTION_URL_PLACEHOLDER));
});

test('deck processor failures get a human headline and technical detail', () => {
	const result = presentation.formatToastPresentation({
		kind: 'error',
		message: 'Deck 1 processor failed - worklet crashed'
	});
	assert.match(result.headline, /stopped unexpectedly/i);
	assert.match(result.detail ?? '', /processor failed/);
});
