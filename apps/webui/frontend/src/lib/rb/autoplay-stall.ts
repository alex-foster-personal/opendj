/**
 * PLAY-08: the durable description of an AutoPlay stop that will not resume.
 *
 * AutoPlay has several terminal branches - the playlist is spent, every
 * remaining row is a missing/stub file, a handoff failed too many times - and
 * every one of them used to signal exactly once, through a toast that clears
 * itself after TOAST_DEFAULT_MS. Five seconds later nothing on screen said the
 * set had stopped, so the operator's experience was "the app randomly stopped
 * playing" (the maintainer, Wed 9 Sep 2026; incident toast at 18:23:39.614Z followed by
 * 2h29m of silence with no reload).
 *
 * This module is the PURE half: it turns the controller's terminal branch into
 * a descriptor a person can act on - what happened, which tracks are involved,
 * and what to do to get sound back. The controller owns when to raise it
 * (auto-play.svelte.ts) and the reactive holder owns how long it lives
 * (autoplay-stall.svelte.ts).
 *
 * Deliberately NOT a recovery mechanism. When every remaining row is a stub
 * there is nothing playable to play, so stopping is correct; being quiet about
 * it is the defect. See issue #1640.
 */
import type { AutoPlayTrackRow } from '$lib/rb/auto-play-chain';

/** Terminal branches. Each one leaves a loaded deck with no next track. */
export type AutoPlayStallReason =
	| 'missing-audio'
	| 'no-next-in-order'
	| 'no-compatible-track'
	| 'candidates-failed-to-load'
	| 'handoff-attempts-exhausted'
	| 'handoff-incomplete';

export interface AutoPlayStallTrack {
	stable_id: string;
	title: string | null;
	artist: string | null;
}

/**
 * A described stall, before it is recorded.
 *
 * Split from `AutoPlayStall` so the pure module stays pure: the revision that
 * makes one OCCURRENCE distinguishable from another is minted at raise time,
 * by the module that owns the state, not by the function that chooses words.
 */
export interface AutoPlayStallDescription {
	reason: AutoPlayStallReason;
	/**
	 * The track that was playing when AutoPlay gave up.
	 *
	 * This is the clear condition, and it is why it is part of the descriptor
	 * rather than controller-local: the stall ends when sound comes back, which
	 * is observable as AutoPlay seeing a DIFFERENT playing source. The source
	 * merely ENDING is not that, and is precisely the moment the room goes
	 * quiet.
	 */
	source_stable_id: string;
	/** One line naming the cause, shown as the banner headline. */
	headline: string;
	/** What the operator does to get sound back. */
	resume: string;
	/** Extra machine detail (a handoff error message), when the branch has one. */
	detail: string | null;
	/** The DISTINCT tracks AutoPlay could not use, capped for display. */
	blocked: readonly AutoPlayStallTrack[];
	/**
	 * How many distinct tracks there were, so a capped list never reads as the
	 * whole set.
	 *
	 * DISTINCT, not row count (Codex r3973913201): playlist membership is keyed
	 * by position, so the same missing file can occupy several rows. Rendering
	 * that list keyed by `stable_id` is a duplicate-key error, i.e. the banner
	 * fails to draw the one thing it exists to show. It is also the number the
	 * operator needs - they relink FILES, not positions.
	 */
	blocked_total: number;
}

/**
 * A recorded stall. `revision` identifies the OCCURRENCE (Codex r3974381597).
 *
 * Two different stops can share every other field - same reason, same source
 * track, a different playlist underneath - so anything keyed on the CONTENT of
 * a stall treats the second as a continuation of the first. The banner's
 * expanded-list state is exactly such a consumer, and getting it wrong springs
 * a list over the decks that nobody asked for.
 */
export interface AutoPlayStall extends AutoPlayStallDescription {
	revision: number;
}

/**
 * How many blocked rows the descriptor carries.
 *
 * A stalled playlist can hold thousands of rows and this object is rendered
 * into a banner AND mirrored to the engine on every ui-mirror push. The count
 * is always exact; only the naming is capped.
 */
export const STALL_TRACK_LIMIT = 12;

function _headline(reason: AutoPlayStallReason, blockedTotal: number): string {
	switch (reason) {
		case 'missing-audio':
			return blockedTotal === 1
				? 'AutoPlay stopped: the last remaining playlist track has missing or stub audio'
				: `AutoPlay stopped: all ${blockedTotal} remaining playlist tracks have missing or stub audio`;
		case 'no-next-in-order':
			return 'AutoPlay stopped: no next unplayed track in playlist order';
		case 'no-compatible-track':
			return 'AutoPlay stopped: no unplayed playlist track fits key +-1 and the Beat Sync BPM range';
		case 'candidates-failed-to-load':
			return 'AutoPlay stopped: every compatible track it tried failed to load';
		case 'handoff-attempts-exhausted':
			return 'AutoPlay stopped: the next track failed to load or play three times';
		case 'handoff-incomplete':
			return 'AutoPlay stopped: the next track is on a deck but the handoff did not finish';
		default: {
			const _exhaustive: never = reason;
			throw new Error(`unhandled AutoPlay stall reason: ${String(_exhaustive)}`);
		}
	}
}

function _resume(reason: AutoPlayStallReason): string {
	switch (reason) {
		case 'missing-audio':
			return 'Relink or re-download the tracks below, or open a playlist whose files are present, then press play on a deck.';
		case 'no-next-in-order':
			return 'Open a playlist with unplayed tracks, or turn off Enforce play order, then press play on a deck.';
		case 'no-compatible-track':
			// NOT "turn off Enforce play order": the controller only reaches this
			// branch while that setting is already OFF, so naming it would send
			// the operator to a switch that is not the one holding them up.
			return 'Widen the follower deck pitch range, or open a playlist with tracks in a nearby key and tempo, then press play on a deck.';
		case 'candidates-failed-to-load':
			return 'The candidates were compatible and would not load, so key and tempo are not the problem: check those files, then press play on a deck.';
		case 'handoff-attempts-exhausted':
			return 'Load the next track by hand and press play; the failures are in the toast log.';
		case 'handoff-incomplete':
			return 'Press play on the deck that was loaded, or load a track by hand.';
		default: {
			const _exhaustive: never = reason;
			throw new Error(`unhandled AutoPlay stall reason: ${String(_exhaustive)}`);
		}
	}
}

/**
 * Build the descriptor. `blocked` is the FULL remaining candidate set; the cap
 * is applied here so no caller has to remember to apply it.
 */
export function describeAutoPlayStall(input: {
	reason: AutoPlayStallReason;
	source_stable_id: string;
	blocked: readonly AutoPlayTrackRow[];
	detail?: string | null;
}): AutoPlayStallDescription {
	const distinct = new Map(input.blocked.map((row) => [row.stable_id, row] as const));
	const blockedTotal = distinct.size;
	if (input.source_stable_id === '') {
		// The clear condition compares against this id. An empty one would
		// never equal a live source, so the stall could never be retired.
		throw new Error('AutoPlay stall requires the stable_id of the stalled source track');
	}
	if (input.reason === 'missing-audio' && blockedTotal === 0) {
		// The controller only reaches this branch with a non-empty remainder,
		// and a headline reading "all 0 remaining tracks" would be a silent
		// mis-report of a state nothing else describes.
		throw new Error('missing-audio stall requires at least one blocked track');
	}
	return {
		reason: input.reason,
		source_stable_id: input.source_stable_id,
		headline: _headline(input.reason, blockedTotal),
		resume: _resume(input.reason),
		detail: input.detail ?? null,
		blocked: [...distinct.values()].slice(0, STALL_TRACK_LIMIT).map((row) => ({
			stable_id: row.stable_id,
			title: row.title ?? null,
			artist: row.artist ?? null
		})),
		blocked_total: blockedTotal
	};
}

/** One-line label for a blocked row, for the banner and the ui-mirror. */
export function describeStallTrack(track: AutoPlayStallTrack): string {
	if (track.title === null && track.artist === null) return track.stable_id;
	if (track.artist === null) return track.title ?? track.stable_id;
	if (track.title === null) return `${track.artist} - ${track.stable_id}`;
	return `${track.artist} - ${track.title}`;
}
