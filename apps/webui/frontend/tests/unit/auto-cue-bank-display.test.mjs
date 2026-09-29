/**
 * Source-text pins for auto-cue proposals in HotCueBank empty slots (issue #196).
 *
 * Regression lines:
 * - if a proposed pad sets class:filled then a proposal looks committed
 * - if empty mapped click no longer beginRenames then SAVE is stolen
 * - if 404 ANALYSIS_NOT_FOUND toasts or disables pads then missing analysis is treated as a hard error
 * - if proposal !== true still renders then a committed-looking pad can appear
 * - if MIDI or waveform painters gain proposal markers then this issue exceeded slots
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));

function source(relativePath) {
	const text = readFileSync(`${SRC}/${relativePath}`, 'utf8');
	assert.ok(text.length > 0, `${relativePath} read as empty`);
	return text;
}

const HOT_CUE_BANK = 'lib/components/rb/deck/HotCueBank.svelte';
const PROPOSAL_LABEL = 'lib/components/rb/deck/HotCueProposalLabel.svelte';
const CACHE = 'lib/components/rb/deck/auto-cues-cache.svelte.ts';
// canSave is hotCueEditsAllowed(stable_id, has_rb_mapping): see hot-cue-mapping-gate.test.mjs.
const EMPTY_SAVE_GUARD = /if\s*\(\s*deck\.stable_id\s*===\s*null\s*\|\|\s*!canSave\s*\)\s*return/;

// REQ: DECKUX-15
test('class:filled remains committed-cue only; class:proposal is empty-plus-visible', () => {
	const text = source(HOT_CUE_BANK);
	assert.match(
		text,
		/class:filled=\{entry\.cue !== null\}/,
		'class:filled must stay entry.cue !== null only - a proposal must not look committed'
	);
	assert.match(
		text,
		/class:proposal=\{entry\.cue === null && visible !== null\}/,
		'class:proposal must be gated on an empty slot with a visible proposal'
	);
	assert.match(
		text,
		/data-proposal-kind=\{visible !== null \? visible\.kind : undefined\}/,
		'data-proposal-kind must be present on the pad when a proposal is shown'
	);
});

test('onSlotClick still refuses unmapped/unloaded empty slots and still beginRenames mapped empties', () => {
	const text = source(HOT_CUE_BANK);
	const fnStart = text.indexOf('async function onSlotClick');
	assert.ok(fnStart >= 0, 'onSlotClick not found in HotCueBank.svelte');
	const fnEnd = text.indexOf('\n\t}', fnStart);
	assert.ok(fnEnd > fnStart, 'onSlotClick body end not found');
	const fnText = text.slice(fnStart, fnEnd);

	assert.ok(
		EMPTY_SAVE_GUARD.test(fnText),
		'onSlotClick lost EMPTY_SAVE_GUARD - a proposal must not steal the unmapped/unloaded inert path'
	);
	assert.match(
		fnText,
		/await beginRename\(entry\.slot, deck\.position_ms, deck\.stable_id\);/,
		'an empty mapped pad must still SAVE the current playhead, not the proposal time_s'
	);
	assert.doesNotMatch(
		fnText,
		/time_s/,
		'onSlotClick must not write or jump to a proposal time_s'
	);
});

test('empty-slot title binding still carries MAPPING_TIP and NOT_LOADED_TIP', () => {
	const text = source(HOT_CUE_BANK);
	assert.match(text, /const MAPPING_TIP = ['"]cues need a rekordbox mapping['"]/);
	assert.match(text, /const NOT_LOADED_TIP = ['"]no track loaded - nothing to save['"]/);

	const titleStart = text.indexOf('title={entry.cue === null');
	assert.ok(titleStart >= 0, 'the slot title binding no longer starts from the empty-slot case');
	const titleEnd = text.indexOf('onclick=', titleStart);
	assert.ok(titleEnd > titleStart, 'title binding end not found before onclick');
	const titleText = text.slice(titleStart, titleEnd);
	assert.ok(
		titleText.includes('MAPPING_TIP') && titleText.includes('NOT_LOADED_TIP'),
		'MAPPING_TIP and NOT_LOADED_TIP must remain in the empty-slot title binding'
	);
	assert.ok(
		titleText.includes('proposalTitle') &&
			titleText.includes('click to save the current position'),
		'a mapped empty proposal tooltip concatenates proposalTitle with the save hint'
	);
});

test('proposal chrome is a dashed accent border, never solid green', () => {
	let css = source(HOT_CUE_BANK);
	try {
		css += `\n${source(PROPOSAL_LABEL)}`;
	} catch (error) {
		if (error.code !== 'ENOENT') throw error;
	}
	assert.match(
		css,
		/border-left:\s*3px\s+dashed\s+var\(--rb-accent\)/,
		'proposal pads need a 3px dashed --rb-accent left border'
	);
	const proposalBlock = css.match(/\.slot\.proposal[\s\S]{0,240}/);
	assert.ok(proposalBlock, '.slot.proposal CSS rule is missing');
	assert.doesNotMatch(
		proposalBlock[0],
		/--rb-green/,
		'proposal chrome must not reuse the solid green committed pad color'
	);
});

test('cache is ready only for proposal:true and swallows ANALYSIS_NOT_FOUND without a toast', () => {
	const text = source(CACHE);
	assert.match(text, /NOT_A_PROPOSAL/, 'a 200 with proposal !== true must fail closed');
	assert.match(
		text,
		/err instanceof RbApiError/,
		'ANALYSIS_NOT_FOUND and other RbApiError codes cache as error, not ready'
	);
	assert.doesNotMatch(
		text,
		/showToast|pushToast|toast\(/,
		'a missing analysis row must not raise a toast; empty pads stay empty'
	);
	assert.match(
		text,
		/code: 'FETCH_FAILED'/,
		'network/shape failures cache FETCH_FAILED then rethrow'
	);
});

test('this issue does not paint proposals onto MIDI or the waveform', () => {
	const midi = source('lib/rb/performance-ipc.svelte.ts');
	assert.doesNotMatch(midi, /auto-cue|auto_cue|proposalFor|ensureAutoCues/);
	const waveCues = source('lib/components/rb/wave/cues.ts');
	assert.doesNotMatch(waveCues, /auto-cue|ensureAutoCues|proposalCaption/);
});
