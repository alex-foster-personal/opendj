/**
 * Live transition read model (TRANS-01, issue #324).
 *
 * Maps mixer + deck stores into `TransitionInput` and runs the pure
 * classifier. UI (`TransitioningChip`) and `queryPerformanceState()` both
 * call `readTransition()`, so they cannot diverge. This module does not
 * import performance-ipc (that file imports this one).
 *
 * The status light is driven from this function rather than a module-level
 * `$effect`: the node:test harness only substitutes `$state`, and a top-level
 * `$effect` would throw when the query live tests load this module.
 */

import { DECK_IDS } from '$lib/player/constants';
import { isMasterMuted } from '$lib/player/master-mute.svelte';
import { getDeckState, mixerState } from '$lib/player/state.svelte';
import {
	classifyTransition,
	type TransitionDeckInput,
	type TransitionInput,
	type TransitionStatus
} from './transition-classifier';
import { getTransitionStatusLight } from './transition-status-light';

function _deckInput(id: TransitionDeckInput['id']): TransitionDeckInput {
	const deck = getDeckState(id);
	const channel = mixerState.channels[id];
	return {
		id,
		loaded: deck.stable_id !== null,
		playing: deck.playing,
		audible: deck.audible,
		fader: channel.fader,
		trim: channel.trim,
		assign: channel.assign,
		beat_sync_enabled: deck.beat_sync_enabled,
		is_master: deck.is_master,
		position_ms: deck.position_ms,
		cue_ms: deck.cue_ms,
		bpm: deck.bpm,
		phrases: _mappedOnce(_phraseMemo, deck.anlz?.phrases ?? _EMPTY, (phrase) => ({
			start_ms: phrase.start_s * 1000,
			end_ms: phrase.end_s * 1000
		})),
		beatgrid: _mappedOnce(_beatMemo, deck.anlz?.beatgrid.beats ?? _EMPTY, (beat) => ({
			n: beat.n,
			time_ms: beat.t * 1000
		}))
	};
}

// PERF-GRID-03: the chip's `$derived(readTransition())` re-runs on every
// transport tick, and mapping a ~1,000-beat grid through Svelte proxies each
// time was a top main-thread cost with two decks playing. Analysis arrays
// are replaced, never edited in place, so identity plus length keys the copy.
const _EMPTY: readonly never[] = [];
const _phraseMemo = new WeakMap<object, { length: number; mapped: { start_ms: number; end_ms: number }[] }>();
const _beatMemo = new WeakMap<object, { length: number; mapped: { n: number; time_ms: number }[] }>();

function _mappedOnce<S, T>(
	memo: WeakMap<object, { length: number; mapped: T[] }>,
	source: readonly S[],
	map: (item: S) => T
): T[] {
	const hit = memo.get(source);
	if (hit !== undefined && hit.length === source.length) return hit.mapped;
	const mapped = source.map(map);
	memo.set(source, { length: source.length, mapped });
	return mapped;
}

function _input(): TransitionInput {
	return {
		now_ms: Date.now(),
		crossfader: mixerState.crossfader,
		master: mixerState.master,
		master_muted: isMasterMuted(),
		decks: DECK_IDS.map(_deckInput)
	};
}

export function readTransition(): TransitionStatus {
	const status = classifyTransition(_input());
	getTransitionStatusLight().setState(status.state);
	return status;
}
