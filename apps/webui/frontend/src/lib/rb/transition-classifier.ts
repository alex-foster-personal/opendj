/**
 * Dual-deck blend classifier (TRANS-01, issue #324).
 *
 * Pure function of a mixer + transport snapshot. No timers, no Svelte, no
 * PerformanceState. Parking the crossfader or dropping the incoming fader
 * clears `transitioning` on the same call: there is no dwell/hold, because a
 * sticky timer is how you get the false positive the acceptance line forbids.
 *
 * Room-heard gain copies the engine equal-power law (`_xfGainFor`) and the
 * master-path chain in `sets/deck-audibility.ts` (trim * TRIM_MAX_GAIN *
 * fader * xfader * master). This module does not import deck-audibility:
 * that file imports PerformanceState from performance-ipc, and the IPC
 * query path reads this classifier, so the cycle is forbidden. `?extroute=`
 * external-mixer blends are out of scope; this uses the internal master
 * path only. EQ-kill as silence is the same exclusion as deck-audibility.
 */

import { TRIM_MAX_GAIN } from '$lib/player/constants';

export type TransitionState = 'idle' | 'approaching' | 'transitioning';

export interface TransitionDeckInput {
	id: 1 | 2 | 3 | 4;
	loaded: boolean;
	playing: boolean;
	audible: boolean;
	fader: number;
	trim: number;
	assign: 'A' | 'B' | 'THRU';
	beat_sync_enabled: boolean;
	is_master: boolean;
	position_ms: number;
	cue_ms: number | null;
	bpm: number | null;
	phrases: ReadonlyArray<{ start_ms: number; end_ms: number }>;
	beatgrid: ReadonlyArray<{ n: number; time_ms: number }>;
}

export interface TransitionInput {
	now_ms: number;
	crossfader: number;
	master: number;
	master_muted: boolean;
	decks: readonly TransitionDeckInput[];
}

export interface TransitionStatus {
	state: TransitionState;
	incoming_deck: 1 | 2 | 3 | 4 | null;
	outgoing_deck: 1 | 2 | 3 | 4 | null;
}

/** Linear master-path gain that counts as in the room. */
export const HEARD_GAIN = 0.05;

/** Incoming fader/xfader motion above silence but below heard. */
export const APPROACHING_GAIN = 1e-4;

/** Outgoing playhead within this many ms of the next phrase/grid boundary. */
export const PHRASE_WINDOW_MS = 8000;

/** Incoming playing and |position_ms - cue_ms| within this window. */
export const CUE_PLAY_WINDOW_MS = 2000;

/** Crossfader is off-center when outside [0, this] and [1-this, 1]. */
export const XFADER_OFF_CENTER = 0.02;

const IDLE: TransitionStatus = {
	state: 'idle',
	incoming_deck: null,
	outgoing_deck: null
};

interface ScoredDeck {
	deck: TransitionDeckInput;
	gain: number;
	active: boolean;
}

/** Equal-power crossfade gain. Mirrors `_xfGainFor` in audio-engine.svelte.ts. */
function _crossfaderGain(assign: TransitionDeckInput['assign'], x: number): number {
	if (assign === 'THRU') return 1;
	if (assign === 'A') return Math.cos((x * Math.PI) / 2);
	return Math.cos(((1 - x) * Math.PI) / 2);
}

function _masterPathGain(
	deck: TransitionDeckInput,
	crossfader: number,
	master: number
): number {
	return deck.trim * TRIM_MAX_GAIN * deck.fader * _crossfaderGain(deck.assign, crossfader) * master;
}

function _isActive(deck: TransitionDeckInput): boolean {
	return deck.loaded && (deck.playing || deck.audible);
}

/**
 * Next operator blend-window end: soonest phrase edge after the playhead, or
 * (when phrases are empty) the next PQTZ downbeat at least 4 beats ahead.
 * Empty phrases do not invent PSSI; the grid fallback is the only substitute.
 */
function _nextBlendBoundaryMs(deck: TransitionDeckInput): number | null {
	const pos = deck.position_ms;
	if (deck.phrases.length > 0) {
		let next: number | null = null;
		for (const phrase of deck.phrases) {
			for (const edge of [phrase.start_ms, phrase.end_ms]) {
				if (edge > pos && (next === null || edge < next)) next = edge;
			}
		}
		return next;
	}
	const grid = deck.beatgrid;
	if (grid.length === 0) return null;
	let currentIdx = -1;
	for (let i = 0; i < grid.length; i += 1) {
		if (grid[i].time_ms <= pos) currentIdx = i;
		else break;
	}
	const minIdx = currentIdx + 4;
	for (let i = Math.max(0, minIdx); i < grid.length; i += 1) {
		if (grid[i].n === 1) return grid[i].time_ms;
	}
	return null;
}

function _openingAssign(crossfader: number): 'A' | 'B' | null {
	if (crossfader <= XFADER_OFF_CENTER || crossfader >= 1 - XFADER_OFF_CENTER) return null;
	if (crossfader < 0.5) return 'B';
	if (crossfader > 0.5) return 'A';
	return null;
}

function _nearCuePlay(deck: TransitionDeckInput): boolean {
	if (!deck.playing || deck.cue_ms === null) return false;
	return Math.abs(deck.position_ms - deck.cue_ms) <= CUE_PLAY_WINDOW_MS;
}

function _isApproachingIncoming(
	candidate: ScoredDeck,
	outgoing: ScoredDeck,
	input: TransitionInput
): boolean {
	const deck = candidate.deck;
	if (candidate.active && candidate.gain < HEARD_GAIN) return true;
	if (deck.fader > APPROACHING_GAIN && deck.fader < HEARD_GAIN) return true;
	const opening = _openingAssign(input.crossfader);
	if (opening !== null && deck.assign === opening) return true;
	if (_nearCuePlay(deck)) return true;
	if (deck.beat_sync_enabled && !deck.is_master) return true;
	const boundary = _nextBlendBoundaryMs(outgoing.deck);
	return boundary !== null && boundary - outgoing.deck.position_ms <= PHRASE_WINDOW_MS;
}

function _pickOutgoing(heard: readonly ScoredDeck[]): ScoredDeck {
	return [...heard].sort((a, b) => {
		if (b.gain !== a.gain) return b.gain - a.gain;
		return a.deck.id - b.deck.id;
	})[0];
}

function _pickIncoming(candidates: readonly ScoredDeck[]): ScoredDeck {
	return [...candidates].sort((a, b) => {
		if (b.gain !== a.gain) return b.gain - a.gain;
		const aFollow = a.deck.beat_sync_enabled && !a.deck.is_master ? 1 : 0;
		const bFollow = b.deck.beat_sync_enabled && !b.deck.is_master ? 1 : 0;
		if (bFollow !== aFollow) return bFollow - aFollow;
		return a.deck.id - b.deck.id;
	})[0];
}

export function classifyTransition(input: TransitionInput): TransitionStatus {
	// Cue-to-play is snapshot-local (|position - cue|); now_ms is for the
	// input contract only and must not become a sticky wall-clock map.
	void input.now_ms;
	if (input.master_muted) return IDLE;

	const scored: ScoredDeck[] = input.decks.map((deck) => ({
		deck,
		gain: _masterPathGain(deck, input.crossfader, input.master),
		active: _isActive(deck)
	}));
	const heard = scored.filter((row) => row.active && row.gain >= HEARD_GAIN);

	if (heard.length >= 2) {
		const outgoing = _pickOutgoing(heard);
		const incoming = _pickIncoming(heard.filter((row) => row.deck.id !== outgoing.deck.id));
		return {
			state: 'transitioning',
			outgoing_deck: outgoing.deck.id,
			incoming_deck: incoming.deck.id
		};
	}

	if (heard.length !== 1) return IDLE;

	const outgoing = heard[0];
	const matching = scored.filter(
		(row) =>
			row.deck.loaded &&
			row.deck.id !== outgoing.deck.id &&
			_isApproachingIncoming(row, outgoing, input)
	);
	if (matching.length === 0) return IDLE;
	const incoming = _pickIncoming(matching);
	return {
		state: 'approaching',
		outgoing_deck: outgoing.deck.id,
		incoming_deck: incoming.deck.id
	};
}
