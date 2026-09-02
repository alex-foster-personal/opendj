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
 * No DOM-mounting harness exists in this suite (see transport-visual-feedback
 * and library-row-hydration for the same convention), so the gate and the
 * tooltip text are pinned as source text, same as every other structural
 * guard in this file's neighbors.
 *
 * Regression lines:
 * - if onSlotClick fires onSave for an empty slot while deck.has_rb_mapping is
 *   false then a SAVE request reaches the server and 404s
 * - if a FILLED slot is also gated on has_rb_mapping then jumping to an
 *   existing cue breaks on an unmapped deck, which is not the bug being fixed
 * - if the tooltip does not change for the inert case then the control is
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

test('onSlotClick refuses an empty slot on an unmapped deck before it can save', () => {
	const text = source(HOT_CUE_BANK);
	const fnStart = text.indexOf('async function onSlotClick');
	assert.ok(fnStart >= 0, 'onSlotClick not found in HotCueBank.svelte');
	const fnEnd = text.indexOf('\n\t}', fnStart);
	assert.ok(fnEnd > fnStart, 'onSlotClick body end not found');
	const fnText = text.slice(fnStart, fnEnd);

	assert.ok(
		/if\s*\(\s*entry\.cue\s*===\s*null\s*&&\s*!deck\.has_rb_mapping\s*\)\s*return/.test(fnText),
		'onSlotClick no longer refuses an empty slot on an unmapped deck - a click can reach ' +
			'onSave and fire a djmdCue write that has no djmdContent row to land in (404)'
	);

	// The guard must be scoped to `entry.cue === null` (an EMPTY slot). A
	// filled slot's onclick still needs to jump, regardless of has_rb_mapping.
	const guardIndex = fnText.search(/if\s*\(\s*entry\.cue\s*===\s*null\s*&&\s*!deck\.has_rb_mapping/);
	const jumpIndex = fnText.indexOf('onJump(entry.cue.in_ms)');
	assert.ok(jumpIndex > guardIndex, 'the jump branch must still run after the mapping guard');
});

test('an empty slot on an unmapped deck carries an explanatory tooltip, not a bare inert control', () => {
	const text = source(HOT_CUE_BANK);

	assert.match(
		text,
		/const MAPPING_TIP = ['"]cues need a rekordbox mapping['"]/,
		'MAPPING_TIP constant missing or reworded - PARITY-TODO.md line 134 names this exact tooltip'
	);

	const titleStart = text.indexOf('title={entry.cue === null');
	assert.ok(titleStart >= 0, 'the slot title binding no longer starts from the empty-slot case');
	const titleEnd = text.indexOf('onclick=', titleStart);
	assert.ok(titleEnd > titleStart, 'title binding end not found before onclick');
	const titleText = text.slice(titleStart, titleEnd);

	assert.ok(
		titleText.includes('deck.has_rb_mapping') && titleText.includes('MAPPING_TIP'),
		'the empty-slot title no longer branches on deck.has_rb_mapping to show MAPPING_TIP - ' +
			'an inert slot with no explanation is exactly what the no-mocked-data rule forbids'
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
