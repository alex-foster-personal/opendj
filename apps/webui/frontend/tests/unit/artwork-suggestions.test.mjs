/** LIBM-189: store covers wait on the user's consent. */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const lib = await loadTypeScriptModule('src/lib/enrich/artwork-suggestions.ts');
const card = await loadTypeScriptModule('src/lib/enrich/enrich-card.ts');

test('the card line counts the covers waiting, and says nothing when none wait', () => {
	assert.equal(lib.suggestionsLine(0), null);
	assert.equal(lib.suggestionsLine(1), '1 cover was found on Deezer/iTunes and wait for your OK');
	assert.match(lib.suggestionsLine(8), /^8 covers were found/);
});

test('the dialog opens by itself once a session, never while a deck plays', () => {
	assert.equal(lib.shouldAutoOpen(3, false, false), true);
	assert.equal(lib.shouldAutoOpen(3, true, false), false, 'never on top of a live set');
	assert.equal(lib.shouldAutoOpen(3, false, true), false, 'once a session');
	assert.equal(lib.shouldAutoOpen(0, false, false), false);
});

test('each button sends its own decide body; Save selected and Skip act on the ticked only', () => {
	const ticked = new Set(['a', 'b']);
	const none = { accept: [], skip: [], accept_all: false, skip_all: false };
	assert.deepEqual(lib.decideBody('save-selected', ticked), { ...none, accept: ['a', 'b'] });
	assert.deepEqual(lib.decideBody('save-all', ticked), { ...none, accept_all: true });
	assert.deepEqual(lib.decideBody('skip', ticked), { ...none, skip: ['a', 'b'] });
});

test('covers waiting on consent flag the enrich pill', () => {
	const summary = {
		show: true,
		analysis: null,
		analysis_error: null,
		coverage: null,
		coverage_error: null,
		stems: { state: 'done', pending: 0, reason: null },
		decisions: {},
		artwork_suggestions: 2
	};
	assert.equal(card.needsAttention(summary, null, null), true);
});
