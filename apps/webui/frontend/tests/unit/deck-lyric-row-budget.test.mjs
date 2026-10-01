import assert from 'node:assert/strict';
import { test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

test('deckLyricRowBudget maps cue-flex height to 1-3 rows', async () => {
	const { deckLyricRowBudget } = await loadTypeScriptModule(
		'src/lib/rb/lyrics/deck-lyric-row-budget.ts'
	);
	assert.equal(deckLyricRowBudget(0), 1);
	assert.equal(deckLyricRowBudget(40), 1);
	assert.equal(deckLyricRowBudget(55), 2);
	assert.equal(deckLyricRowBudget(80), 3);
});
