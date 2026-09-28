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
