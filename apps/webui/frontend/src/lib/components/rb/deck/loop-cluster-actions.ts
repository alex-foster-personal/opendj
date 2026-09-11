/**
 * Loop cluster pure command-construction + enablement/tooltip logic, split
 * out of LoopCluster.svelte's script (a distinct concern from the button
 * markup and wiring - none of this touches the DOM or the deck prop
 * callbacks, it only computes what the component should show or send).
 * Plain functions over explicit inputs so they stay unit-testable without
 * mounting the component.
 */
import type { AnlzBeat } from '$lib/rb/anlz-types';
import type { DeckState } from '$lib/rb/deck-state-types';
import { beatLoopFitsWithinDuration, resizedLoopRangeMs } from '$lib/player/transport/loops';

/** Which side of the loop stays fixed while resizing, from click modifiers:
 * plain click keeps loop-in fixed (existing behaviour), shift-click keeps
 * loop-out fixed and moves loop-in instead, option/alt-click keeps the
 * midpoint fixed and moves both ends by half the change (Photoshop-style
 * centered transform). Both held is alt/option wins. */
export function resizeAnchorOf(event: MouseEvent): 'start' | 'end' | 'center' {
	if (event.altKey) return 'center';
	if (event.shiftKey) return 'end';
	return 'start';
}

/** The onEngage(beats, startMs) args a halve/double resize should send to
 * reapply `beatLength` against the live loop, anchored per `anchor`. Null
 * when there is no live loop to resize, or (end/center anchors) when the
 * grid does not have enough beats either side of the anchor to split at
 * this length - same "does it fit from here" guard as
 * beatLoopFitsWithinDuration, just computed lazily since it depends on
 * which modifier was held at click time rather than on render. */
export function reapplyEngageArgs(args: {
	anchor: 'start' | 'end' | 'center';
	deck: DeckState;
	gridBeats: readonly AnlzBeat[];
	beatLength: number;
}): { beats: number; startMs?: number } | null {
	const { anchor, deck, gridBeats, beatLength } = args;
	if (deck.loop === null) return null;
	if (anchor === 'start' || deck.duration_ms === null) {
		// Resize a live loop keeping its in point.
		return { beats: beatLength, startMs: deck.loop.in_ms };
	}
	try {
		const range = resizedLoopRangeMs(gridBeats, deck.loop, beatLength, anchor, deck.duration_ms);
		return { beats: beatLength, startMs: range.in_ms };
	} catch {
		// Not enough grid either side to split this anchor at this length.
		return null;
	}
}

/** The onEngage(beats, startMs) args that reissue the live loop's own beat
 * length + in point unchanged. The engine recognises the exact match as a
 * restart rather than a no-op re-engage: it jumps the playhead back to
 * loop-in and keeps looping. Null when there is no engaged exact-beat-count
 * loop to restart. */
export function restartEngageArgs(deck: DeckState): { beats: number; startMs: number } | null {
	if (deck.loop === null || deck.loop.beat_length === null) return null;
	return { beats: deck.loop.beat_length, startMs: deck.loop.in_ms };
}

/** A choice is engageable when `n` beats actually fit from the loop
 * anchor (the engaged loop's in point, or the live position otherwise)
 * AND end at or before the decoded duration - not merely when the total
 * grid is longer than `n`, which can be true with fewer than `n` beats
 * left ahead near the end of a track, or true only because the engine
 * would silently clip the engaged loop shorter than `n` beats.
 *
 * The currently engaged length is always choosable regardless of that
 * fit check: a loop engaged by another surface (e.g. a direct
 * `beat_loop` command) can report a `beat_length` the grid's own fit
 * math would refuse, since the engine clips the endpoint to duration
 * rather than rejecting it. Without this exception that choice renders
 * `selected` but `disabled`, and the disengage branch of choosing it
 * becomes unreachable - there is no way to exit that loop from here. */
export function canChooseInterval(args: {
	n: number;
	pending: boolean;
	deck: DeckState;
	engaged: boolean;
	engagedIntervalLength: number | null;
	gridBeats: readonly AnlzBeat[];
}): boolean {
	const { n, pending, deck, engaged, engagedIntervalLength, gridBeats } = args;
	if (pending || deck.stable_id === null || deck.duration_ms === null) return false;
	if (engagedIntervalLength === n) return true;
	const startMs = engaged && deck.loop !== null ? deck.loop.in_ms : undefined;
	return beatLoopFitsWithinDuration(gridBeats, deck.position_ms, n, deck.duration_ms, startMs);
}

export function intervalChoiceTip(args: {
	n: number;
	pending: boolean;
	deck: DeckState;
	gridBeatCount: number;
	engagedIntervalLength: number | null;
	canChoose: boolean;
}): string {
	const { n, pending, deck, gridBeatCount, engagedIntervalLength, canChoose } = args;
	if (pending) return 'deck command pending';
	if (deck.stable_id === null) return 'no track loaded';
	if (gridBeatCount === 0) return 'track has no beatgrid - beat loop unavailable';
	if (!canChoose) return `${n} beatgrid beats do not fit from here`;
	if (engagedIntervalLength === n) return 'exit loop';
	return `loop ${n} beats`;
}

export function loopDisabledTip(args: {
	pending: boolean;
	deck: DeckState;
	beatLength: number;
}): string {
	const { pending, deck, beatLength } = args;
	if (pending) return 'deck command pending';
	if (deck.stable_id === null) return 'no track loaded';
	if (deck.anlz === null || deck.anlz.beatgrid.beats.length === 0) {
		return 'track has no beatgrid - beat loop unavailable';
	}
	if (deck.anlz.beatgrid.beats.length <= beatLength) {
		return `beatgrid has too few beats for a ${beatLength}-beat loop`;
	}
	return '';
}

export function loopRestartTip(args: {
	pending: boolean;
	engaged: boolean;
	canRestart: boolean;
}): string {
	const { pending, engaged, canRestart } = args;
	if (canRestart) return 'restart loop: jump back to the loop start and keep looping';
	if (pending) return 'deck command pending';
	if (!engaged) return 'engage a loop first - there is nothing to restart';
	return 'restart unavailable - this loop is not an exact beat-count loop';
}

export function safetyLoopTip(args: { pending: boolean; canSaveSafety: boolean }): string {
	const { pending, canSaveSafety } = args;
	if (canSaveSafety) return 'save the engaged loop as this deck armed safety loop';
	if (pending) return 'deck command pending';
	return 'engage a loop first - there is nothing to save';
}
