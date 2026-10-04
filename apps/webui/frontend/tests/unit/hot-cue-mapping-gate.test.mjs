import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

/**
 * CUES-01 (supersedes PARITY-05 / issue #736) - hot cues now live in Open DJ's
 * own cue store in state.db, so a deck with no rekordbox mapping SAVES like
 * any other. The #736 inert-with-tooltip gate on `deck.has_rb_mapping` is
 * gone; these tests pin that it stays gone, because a revived gate would
 * silently make every locally imported track cue-less again.
 *
 * Issue #804 - `has_rb_mapping` defaults `true` on an empty deck
 * (`_emptyDeckState`, `state.svelte.ts`), so it alone cannot gate a deck with
 * nothing loaded (or momentarily mid-reload, `stable_id === null`). The
 * guard below also checks `deck.stable_id`, matching the precedent in
 * `WaveRow.svelte`'s `onPointerDown` ("Empty deck rows are inert").
 *
 * No DOM-mounting harness exists in this suite (see transport-visual-feedback
 * and library-row-hydration for the same convention), so the gate and the
 * tooltip text are pinned as source text, same as every other structural
 * guard in this file's neighbors.
 *
 * Regression lines:
 * - if onSlotClick refuses an empty slot because deck.has_rb_mapping is false
 *   then a locally imported track can never keep a cue (CUES-01)
 * - if onSlotClick fires onSave for an empty slot while deck.stable_id is
 *   null then a SAVE runs against a deck with nothing loaded and throws
 *   "hot cue X: deck is not loaded" unhandled (#804)
 * - if a FILLED slot is also gated on has_rb_mapping or stable_id then
 *   jumping to an existing cue breaks, which is not the bug being fixed
 * - if the unloaded-deck tooltip goes away then the control is inert with no
 *   explanation, which the repo's no-mocked-data rule forbids
 */

const SRC = fileURLToPath(new URL('../../src', import.meta.url));

function source(relativePath) {
	const text = readFileSync(`${SRC}/${relativePath}`, 'utf8');
	assert.ok(text.length > 0, `${relativePath} read as empty`);
	return text;
}

const HOT_CUE_BANK = 'lib/components/rb/deck/HotCueBank.svelte';
const DECK_STATE_TYPES = 'lib/rb/deck-state-types.ts';
const STATE = 'lib/player/state.svelte.ts';
const AUDIO_ENGINE = 'lib/rb/audio-engine.svelte.ts';

const EMPTY_SAVE_GUARD = /if\s*\(\s*deck\.stable_id\s*===\s*null\s*\)\s*return/;

test('onSlotClick refuses an empty slot only on an unloaded deck, never for a missing rekordbox mapping', () => {
	const text = source(HOT_CUE_BANK);
	const fnStart = text.indexOf('async function onSlotClick');
	assert.ok(fnStart >= 0, 'onSlotClick not found in HotCueBank.svelte');
	const fnEnd = text.indexOf('\n\t}', fnStart);
	assert.ok(fnEnd > fnStart, 'onSlotClick body end not found');
	const fnText = text.slice(fnStart, fnEnd);

	assert.ok(
		EMPTY_SAVE_GUARD.test(fnText),
		'onSlotClick no longer refuses an empty slot on an unloaded deck - a click can save onto a ' +
			'deck with nothing loaded, throwing "deck is not loaded" unhandled (#804)'
	);
	assert.doesNotMatch(
		fnText,
		/has_rb_mapping/,
		'onSlotClick gates on has_rb_mapping again - cues live in the own store (CUES-01), so an ' +
			'unmapped track must be able to save'
	);

	// The jump branch deliberately precedes the empty-save guard: filled pads
	// are Class A immediate controls even while a rename persistence write is
	// queued, and a filled slot must never be blocked by mapping metadata.
	const guardIndex = fnText.search(EMPTY_SAVE_GUARD);
	const jumpIndex = fnText.indexOf('onJump(entry.slot, pressT0Ms)');
	assert.ok(jumpIndex >= 0 && jumpIndex < guardIndex, 'the jump branch must stay immediate before the empty-save guard');
});

test('onClearClick refuses to fire against a deck with nothing loaded', () => {
	const text = source(HOT_CUE_BANK);
	const fnStart = text.indexOf('async function onClearClick');
	assert.ok(fnStart >= 0, 'onClearClick not found in HotCueBank.svelte');
	const fnEnd = text.indexOf('\n\t}', fnStart);
	assert.ok(fnEnd > fnStart, 'onClearClick body end not found');
	const fnText = text.slice(fnStart, fnEnd);

	assert.match(
		fnText,
		/if\s*\(\s*deck\.stable_id\s*===\s*null\s*\)\s*return/,
		'onClearClick lost its stable_id guard - not reachable today (the x only renders for a ' +
			'filled slot, which an empty deck never has), but closing it defensively is the point (#804)'
	);
});

test('an empty slot on an unloaded deck explains itself, and no mapping tooltip remains', () => {
	const text = source(HOT_CUE_BANK);

	assert.doesNotMatch(
		text,
		/cues need a rekordbox mapping/,
		'the #736 mapping tooltip is back - cues live in the own store now (CUES-01)'
	);
	assert.match(
		text,
		/const NOT_LOADED_TIP = ['"]no track loaded - nothing to save['"]/,
		'NOT_LOADED_TIP constant missing or reworded - #804 needs an explanation for the empty-deck case'
	);

	const titleStart = text.indexOf('title={entry.cue === null');
	assert.ok(titleStart >= 0, 'the slot title binding no longer starts from the empty-slot case');
	const titleEnd = text.indexOf('onclick=', titleStart);
	assert.ok(titleEnd > titleStart, 'title binding end not found before onclick');
	const titleText = text.slice(titleStart, titleEnd);

	assert.ok(
		titleText.includes('deck.stable_id') && titleText.includes('NOT_LOADED_TIP'),
		'the empty-slot title no longer branches on deck.stable_id to show NOT_LOADED_TIP - an ' +
			'empty-deck pad would go back to promising a save it cannot perform (#804)'
	);
	assert.ok(
		!titleText.includes('has_rb_mapping'),
		'the empty-slot title branches on has_rb_mapping again (CUES-01 removed that gate)'
	);
	assert.ok(
		text.includes('class:inert-mapping={entry.cue === null && deck.stable_id === null}'),
		'the inert CSS class must cover exactly the empty-deck case (#804), not unmapped tracks'
	);
});

test('DeckState carries has_rb_mapping as a plain boolean, not optional', () => {
	const text = source(DECK_STATE_TYPES);
	assert.match(
		text,
		/has_rb_mapping:\s*boolean;/,
		'DeckState.has_rb_mapping missing or made optional - HotCueBank reads it unguarded'
	);
});

test('a fresh or cleared deck defaults has_rb_mapping true, so an empty deck never reads as unmapped', () => {
	const emptyDeck = source(STATE);
	const emptyFnStart = emptyDeck.indexOf('export function _emptyDeckState');
	assert.ok(emptyFnStart >= 0, '_emptyDeckState not found');
	const emptyFnEnd = emptyDeck.indexOf('\n}', emptyFnStart);
	const emptyFnText = emptyDeck.slice(emptyFnStart, emptyFnEnd);
	assert.match(
		emptyFnText,
		/has_rb_mapping:\s*true/,
		'_emptyDeckState no longer defaults has_rb_mapping true - a brand-new deck would read as ' +
			'unmapped and every empty-slot click would be wrongly inert before any track ever loads'
	);

	const engine = source(AUDIO_ENGINE);
	const clearFnStart = engine.indexOf('function _clearLoadedTrackState');
	assert.ok(clearFnStart >= 0, '_clearLoadedTrackState not found');
	const clearFnEnd = engine.indexOf('\n}', clearFnStart);
	const clearFnText = engine.slice(clearFnStart, clearFnEnd);
	assert.match(
		clearFnText,
		/st\.has_rb_mapping\s*=\s*true/,
		'_clearLoadedTrackState no longer resets has_rb_mapping to true - unloading an unmapped ' +
			"track would leave the NEXT deck's empty state permanently gated"
	);
});

test('deck load copies the fetched track\'s has_rb_mapping onto deck state', () => {
	const engine = source(AUDIO_ENGINE);
	assert.match(
		engine,
		/st\.has_rb_mapping\s*=\s*candidateTrack\.has_rb_mapping/,
		'deck load no longer copies Track.has_rb_mapping onto DeckState - the gate would read a ' +
			'stale value carried over from whatever track loaded previously on this deck'
	);
});
