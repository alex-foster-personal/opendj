import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

/**
 * PARITY-05 / issue #736 - hot-cue SAVE for a deck with no live rekordbox
 * mapping. `djmdCue` is keyed by `djmdContent.ID`, which a locally imported
 * track never has, so a SAVE write for such a deck has nowhere to land and
 * would 404. Rather than let the click fire that write, an empty slot on an
 * unmapped deck (`deck.has_rb_mapping === false`) goes inert-with-tooltip
 * instead (PARITY-TODO.md line 134).
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
 * Play from USB (specs/usb-play-from-stick.md 4b, decision 2): a stick track
 * has no rekordbox mapping and needs none, because its cue edits stay in the
 * session. The gate is therefore `canSave`, derived from the shared
 * `hotCueEditsAllowed(stable_id, has_rb_mapping)` in lib/rb/track-source.ts,
 * whose behavior (library unmapped refused, stick allowed, empty deck
 * refused) is unit-tested in usb-stick-session-edits.test.mjs. This file pins
 * that HotCueBank actually uses it.
 *
 * Regression lines:
 * - if onSlotClick fires onSave for an empty slot while deck.has_rb_mapping is
 *   false then a SAVE request reaches the server and 404s
 * - if onSlotClick fires onSave for an empty slot while deck.stable_id is
 *   null then a SAVE runs against a deck with nothing loaded and throws
 *   "hot cue X: deck is not loaded" unhandled (#804)
 * - if a FILLED slot is also gated on has_rb_mapping or stable_id then
 *   jumping to an existing cue breaks, which is not the bug being fixed
 * - if the tooltip does not change for either inert case then the control is
 *   inert with no explanation, which the repo's no-mocked-data rule forbids
 * - if DeckState drops has_rb_mapping, or a fresh/cleared deck does not
 *   default it true, then every empty deck reads as unmapped and every first
 *   empty-slot click on a freshly loaded MAPPED track is wrongly inert
 * - if the deck load path stops copying Track.has_rb_mapping onto DeckState
 *   then the gate reads a stale value from whatever track loaded previously
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

const EMPTY_SAVE_GUARD = /if\s*\(\s*deck\.stable_id\s*===\s*null\s*\|\|\s*!canSave\s*\)\s*return/;
const CAN_SAVE = /const canSave = \$derived\(hotCueEditsAllowed\(deck\.stable_id, deck\.has_rb_mapping\)\);/;

test('onSlotClick refuses an empty slot on an unmapped OR unloaded deck before it can save', () => {
	const text = source(HOT_CUE_BANK);
	const fnStart = text.indexOf('async function onSlotClick');
	assert.ok(fnStart >= 0, 'onSlotClick not found in HotCueBank.svelte');
	const fnEnd = text.indexOf('\n\t}', fnStart);
	assert.ok(fnEnd > fnStart, 'onSlotClick body end not found');
	const fnText = text.slice(fnStart, fnEnd);

	assert.match(text, CAN_SAVE, 'canSave no longer derives from the shared hotCueEditsAllowed gate on has_rb_mapping');
	assert.ok(
		EMPTY_SAVE_GUARD.test(fnText),
		'onSlotClick no longer refuses an empty slot on an unmapped-or-unloaded deck - a click can ' +
			'reach onSave and either fire a djmdCue write with nowhere to land (404, #736) or save ' +
			'onto a deck with nothing loaded, throwing "deck is not loaded" unhandled (#804)'
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

test('an empty slot on an unmapped or unloaded deck carries an explanatory tooltip, not a bare inert control', () => {
	const text = source(HOT_CUE_BANK);

	assert.match(
		text,
		/const MAPPING_TIP = ['"]cues need a rekordbox mapping['"]/,
		'MAPPING_TIP constant missing or reworded - PARITY-TODO.md line 134 names this exact tooltip'
	);
	assert.match(
		text,
		/const NOT_LOADED_TIP = ['"]no track loaded - nothing to save['"]/,
		'NOT_LOADED_TIP constant missing or reworded - #804 needs a distinct explanation for the ' +
			'empty-deck case, not a reused mapping tooltip that would be factually wrong'
	);

	const titleStart = text.indexOf('title={entry.cue === null');
	assert.ok(titleStart >= 0, 'the slot title binding no longer starts from the empty-slot case');
	const titleEnd = text.indexOf('onclick=', titleStart);
	assert.ok(titleEnd > titleStart, 'title binding end not found before onclick');
	const titleText = text.slice(titleStart, titleEnd);

	assert.ok(
		titleText.includes('canSave') && titleText.includes('MAPPING_TIP'),
		'the empty-slot title no longer branches on canSave (has_rb_mapping) to show MAPPING_TIP - ' +
			'an inert slot with no explanation is exactly what the no-mocked-data rule forbids'
	);
	assert.ok(
		titleText.includes('deck.stable_id') && titleText.includes('NOT_LOADED_TIP'),
		'the empty-slot title no longer branches on deck.stable_id to show NOT_LOADED_TIP - an ' +
			'empty-deck pad would go back to promising a save it cannot perform (#804)'
	);

	assert.ok(
		text.includes('class:inert-mapping={entry.cue === null && !canSave}') && CAN_SAVE.test(text),
		'the inert-mapping CSS class no longer covers the empty-deck case - the pad would render ' +
			'as a normal, live-looking control while still being unable to save (#804)'
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
