import assert from 'node:assert/strict';
import { test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

const MOD = 'src/lib/rb/lyrics/deck-lyric-row-budget.ts';

test('deckLyricRowBudget gives two lyric rows only when the pads keep their minimum', async () => {
	const { deckLyricRowBudget, PAD_BANK_MIN_PX, LYRIC_TWO_ROW_PX } = await loadTypeScriptModule(MOD);
	const edge = PAD_BANK_MIN_PX + LYRIC_TWO_ROW_PX;
	assert.equal(deckLyricRowBudget(0), 1);
	assert.equal(deckLyricRowBudget(Number.NaN), 1);
	assert.equal(deckLyricRowBudget(edge - 1), 1, 'one px short of pads + two rows: compact');
	assert.equal(deckLyricRowBudget(edge), 2);
	// The canonical 4-deck layout measures a 117px cue column.
	assert.equal(deckLyricRowBudget(117), 2);
	// Never more than two: a third row would come out of the pads (LYR-14).
	assert.equal(deckLyricRowBudget(400), 2);
});

test('[if] spare height below a tall pad bank cannot fit 34px [then] budget stays one row', async () => {
	const { deckLyricRowBudget, LYRIC_TWO_ROW_PX } = await loadTypeScriptModule(MOD);
	assert.equal(deckLyricRowBudget(100, 80), 1, 'only 20px spare, not a two-row strip');
	assert.equal(deckLyricRowBudget(80 + LYRIC_TWO_ROW_PX, 80), 2, '34px spare under measured pads');
});

test('[if] cue-flex height can spare 34px under 60px pads [then] budget is two rows', async () => {
	const { deckLyricRowBudget, PAD_BANK_MIN_PX, LYRIC_TWO_ROW_PX } = await loadTypeScriptModule(MOD);
	assert.equal(deckLyricRowBudget(PAD_BANK_MIN_PX + LYRIC_TWO_ROW_PX), 2);
});

test('deckLyricStripPx sizes the strip for one or two rows', async () => {
	const { deckLyricStripPx, LYRIC_ONE_ROW_PX, LYRIC_TWO_ROW_PX } = await loadTypeScriptModule(MOD);
	assert.equal(deckLyricStripPx(2), LYRIC_TWO_ROW_PX);
	assert.equal(deckLyricStripPx(1), LYRIC_ONE_ROW_PX);
	assert.ok(LYRIC_ONE_ROW_PX < LYRIC_TWO_ROW_PX);
});
