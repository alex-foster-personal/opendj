/**
 * Which deck features a loaded track's PQTZ grid can actually support.
 *
 * Requirements:
 *   ✔︎ ✅ 🎯 Never gate transport on a grid-dependent feature.
 *     [if] play, pause or cue is asked of a gridless deck [then] it runs unquantized
 *   ✔︎ ✅ 🎯 Fold the grid check into the quantize / beat-sync flags, not into the flags' values.
 *     [if] a track with no real grid is loaded [then] both flags keep their value and stop taking effect
 *   ✔︎ ✅ 🎯 Give every inert sync / quantize control one shared sentence.
 *     [if] two controls explain the same inert state [then] they must read identically
 *
 * quantize_enabled and beat_sync_enabled both DEFAULT to true, which is
 * rekordbox parity for an analysed library. Those defaults used to reach
 * _requireBeatGrid from inside play() and pause(), so a first-run user who
 * imported an unanalysed file could neither start a deck nor stop one that was
 * already playing. The rule that replaces it: a flag says what the DJ wants,
 * the grid says whether it can happen, and only the SECOND of those may ever
 * stop transport - which it never does.
 */

import { validateBeatGrid } from '$lib/rb/beat-sync-math';
import type { AnlzBeat, AnlzData } from '$lib/rb/anlz-types';
import type { DeckState } from '$lib/rb/deck-state-types';

/**
 * The one sentence every inert sync / quantize control shows on hover when the
 * track simply has no usable grid.
 *
 * Deliberately NOT the 'not implemented - see PARITY-TODO' stub wording, which
 * means "never built" and is policed as a literal by inert-controls.test.mjs.
 * Quantize and Beat Sync are built and work; this track just has nothing for
 * them to lock to, which is a different fact and earns its own sentence. It
 * also names what still works, because the failure this replaces was a user
 * concluding the deck itself was broken.
 */
export const GRID_FEATURE_TIP =
	'needs a beat grid - analyse this track for quantize and beat sync; play, pause and cue still work';

const _NAMED_SOURCES = new Set<string>(['own', 'rekordbox']);

/**
 * Whether these beats are a real PQTZ grid the beat math can plan against.
 *
 * A predicate around the validator rather than a second copy of its rules, so
 * "usable grid" cannot mean one thing here and another inside the math. The
 * catch is the conversion from validator-throws to predicate-returns and
 * nothing else: no caller loses an error it would otherwise have seen, because
 * every caller of this function is deciding, not executing.
 */
export function hasRealBeatGrid(
	beats: readonly AnlzBeat[] | null | undefined
): beats is readonly AnlzBeat[] {
	if (beats === null || beats === undefined) return false;
	try {
		validateBeatGrid(beats);
	} catch {
		return false;
	}
	return true;
}

function _beatgridSource(anlz: Pick<AnlzData, 'beatgrid'> | null | undefined): unknown {
	return anlz?.beatgrid?.source;
}

/** Fail-closed trust predicate for grid-dependent controls (NATIVE-01/03).
 *
 * Identifies the selected source ONLY from `anlz.beatgrid.source`, never from
 * the presence or absence of any other field. Any value other than the two
 * named literals is its own explicit untrusted branch. An own `status: ok` grid
 * with several tempo anchors is trusted when `static_grid_untrusted` is not
 * `true` (omitted on the multi-anchor `/anlz` wire, `false` on the record).
 * Do not treat `tempo_changes.length > 0` as no grid.
 */
export function hasTrustedBeatGrid(anlz: Pick<AnlzData, 'beatgrid'> | null | undefined): boolean {
	if (anlz === null || anlz === undefined) return false;
	const source = _beatgridSource(anlz);
	if (typeof source !== 'string' || !_NAMED_SOURCES.has(source)) {
		return false;
	}
	if (!hasRealBeatGrid(anlz.beatgrid.beats)) return false;
	if (source === 'rekordbox') return true;
	const bg = anlz.beatgrid;
	if (bg.status !== 'ok') return false;
	if (bg.static_grid_untrusted === true) return false;
	return true;
}

export function deckHasRealBeatGrid(st: Pick<DeckState, 'anlz'>): boolean {
	return hasRealBeatGrid(st.anlz?.beatgrid.beats);
}

export function deckHasTrustedBeatGrid(st: Pick<DeckState, 'anlz'>): boolean {
	return hasTrustedBeatGrid(st.anlz);
}

/** Whether beat ticks and tempo-change markers should paint on the deck.
 *
 * Own `status: failed` paints NO beats (never rekordbox's in its place).
 * Own `static_grid_untrusted: true` still paints ticks and tempo markers.
 */
export function shouldPaintBeatGrid(anlz: Pick<AnlzData, 'beatgrid'> | null | undefined): boolean {
	if (anlz === null || anlz === undefined) return false;
	const source = _beatgridSource(anlz);
	if (source === 'own' && anlz.beatgrid.status === 'failed') return false;
	return hasRealBeatGrid(anlz.beatgrid.beats);
}

/** Hover title for inert quantize / beat-sync controls on a loaded deck. */
export function gridFeatureInertTip(
	st: Pick<DeckState, 'anlz' | 'stable_id'>
): string {
	if (st.stable_id === null) return GRID_FEATURE_TIP;
	const anlz = st.anlz;
	if (anlz === null || anlz === undefined) return GRID_FEATURE_TIP;
	const source = _beatgridSource(anlz);
	if (source === 'own' && anlz.beatgrid.status === 'failed') {
		const reason = anlz.beatgrid.reason;
		if (typeof reason === 'string' && reason.length > 0) return reason;
		return 'own beatgrid analysis failed';
	}
	if (source === 'own' && anlz.beatgrid.static_grid_untrusted === true) {
		const first = anlz.tempo_changes?.[0];
		if (first !== undefined) {
			return `static grid untrusted - tempo change at ${first.at_s.toFixed(3)}s`;
		}
		return 'static grid untrusted - tempo change detected';
	}
	return GRID_FEATURE_TIP;
}

/** Quantize as the transport actually applies it: what the DJ asked for, AND
 * a trusted grid to snap to. */
export function effectiveQuantize(st: Pick<DeckState, 'anlz' | 'quantize_enabled'>): boolean {
	return st.quantize_enabled && deckHasTrustedBeatGrid(st);
}

/** Beat Sync as the transport actually applies it: what the DJ asked for, AND
 * a trusted grid to phase-lock with. BAR planning, not this gate, filters
 * `beatIsExtrapolated` anchors at plan time. */
export function effectiveBeatSync(st: Pick<DeckState, 'anlz' | 'beat_sync_enabled'>): boolean {
	return st.beat_sync_enabled && deckHasTrustedBeatGrid(st);
}

/**
 * PLAY-25: may this follower phase-lock to that master right now?
 *
 * Both decks need a trusted grid: the follower's own (`effectiveBeatSync`) AND
 * the master's. Tue 6 Oct 2026, silver preview 17:35Z: with Beat Sync Max on,
 * AutoPlay loaded the next track on deck 1 while the playing master (deck 2)
 * had a 0-beat grid. `play` joined deck 1 to deck 2's phase, `requireBeatGrid`
 * threw on the master, the handoff aborted after the load, and the set went
 * silent when deck 2 ran out. A gridless master means the follower starts
 * unsynced, never that it does not start.
 */
export function canPhaseLockTo(
	follower: Pick<DeckState, 'anlz' | 'beat_sync_enabled'>,
	master: Pick<DeckState, 'anlz'>
): boolean {
	return effectiveBeatSync(follower) && deckHasTrustedBeatGrid(master);
}

/**
 * Whether a LOADED track's missing or untrusted grid is what makes this deck's
 * sync and quantize controls inert.
 *
 * An empty deck is deliberately excluded: nothing has been loaded to analyse,
 * so blaming the grid would send a new user looking for a track that is not
 * there. Empty decks keep their existing controls and copy.
 */
export function gridFeaturesInert(st: Pick<DeckState, 'anlz' | 'stable_id'>): boolean {
	return st.stable_id !== null && !deckHasTrustedBeatGrid(st);
}

// ----- PLAY-27: a follower that cannot phase-lock still starts, unsynced -----
// Lives beside canPhaseLockTo (PLAY-25), its gridless-master twin, so the engine
// gains no import edge for it.
/*
 * PLAY-27: a follower that cannot phase-lock still starts, unsynced.
 *
 * Tue 6 Oct 2026 21:21:58Z, silver preview, build 18: AutoPlay loaded the next
 * track on deck 1, then `play` tried to join the master's phase and the Beat
 * Sync planner refused ("follower grid has no phase-capable bar anchor with
 * tempo ratio within [0.84, 1.16] for beat n=1"). The refusal was thrown out
 * of `play`, the follower never started, and the room went silent (#5735).
 * PLAY-25 had closed the same hole for a gridless MASTER; this closes it for
 * every plan the follower side cannot satisfy: a tempo ratio outside the
 * pitch window, no phase-capable bar anchor, or a follower grid the planner
 * rejects.
 *
 * Only a plan refusal ({@link BeatSyncPhaseLockError}) falls back. Anything
 * else (no audio context, a master that is not playing, a schedule failure)
 * still throws, because starting anyway would hide a real fault.
 *
 * Requirements (mini-PRD):
 *   ✔︎ A plan refusal starts the follower on plain transport.
 *     [if] the planner refuses every follower [then] the follower starts
 *       unsynced and one INFO line names the reason [⛔️ if play throws]
 *   ✔︎ A lockable follower still locks.
 *     [if] the join succeeds [then] plain transport is never scheduled
 *       [⛔️ if a good sync is replaced by an unsynced start]
 *   ✔︎ Other failures stay loud.
 *     [if] the join fails for any other reason [then] it is rethrown and
 *       nothing is started [⛔️ if a real fault is masked]
 */
/** The Beat Sync planner could not phase-lock ANY of the requested followers. */
export class BeatSyncPhaseLockError extends Error {
	/** One entry per follower the planner refused, with the red notice to show for it. */
	readonly failed: readonly UnlockedFollower[];
	constructor(message: string, failed: readonly UnlockedFollower[] = []) {
		super(message);
		this.name = 'BeatSyncPhaseLockError';
		this.failed = failed;
	}
}

/** A follower Beat Sync could not lock, and the notice that says so. */
export interface UnlockedFollower {
	deck: number;
	message: string;
	notice: string;
}

/**
 * The red notice for a follower that plays on unsynced (PLAY-27). The pitch
 * bound stays hard (the maintainer's pin 9bf12adccb45): a tempo the follower cannot
 * reach inside its pitch range is named as a percentage; any other refusal
 * (no phase-capable anchor, a master past its grid) names the planner's reason.
 */
export function unlockedFollowerNotice(
	deck: number,
	follower: Pick<DeckState, 'bpm'> & Partial<Pick<DeckState, 'anlz'>>,
	master: Pick<DeckState, 'bpm' | 'pitch'> & Partial<Pick<DeckState, 'anlz'>>,
	pitchRangePct: number,
	reason: string
): string {
	// The tag when there is one, else the grid's own tempo (a grid-only deck carries no tag).
	const trackBpm = (st: Pick<DeckState, 'bpm'> & Partial<Pick<DeckState, 'anlz'>>): number | null =>
		st.bpm ?? st.anlz?.beatgrid.beats[0]?.bpm ?? null;
	const followerBpm = trackBpm(follower);
	const masterTrackBpm = trackBpm(master);
	const masterBpm = masterTrackBpm === null ? null : masterTrackBpm * master.pitch;
	const range = pitchRangePct / 100;
	if (followerBpm !== null && masterBpm !== null && followerBpm > 0 && masterBpm > 0) {
		const needed = masterBpm / followerBpm;
		if (needed < Math.max(0.01, 1 - range) || needed > 1 + range) {
			const away = Math.round(Math.abs(followerBpm / masterBpm - 1) * 100);
			return `Beat Sync cannot lock deck ${deck}: tempo ${away}% away; playing unsynced`;
		}
	}
	return `Beat Sync cannot lock deck ${deck}: ${reason}; playing unsynced`;
}

/**
 * Join the master's phase, or start unsynced when the planner refuses.
 * Returns how the follower started.
 */
export async function joinPhaseOrStartUnsynced(
	deck: number,
	join: () => Promise<void>,
	startUnsynced: () => Promise<void>,
	log: (line: string) => void = console.info,
	onRefusal: (error: BeatSyncPhaseLockError) => void = () => {}
): Promise<'synced' | 'unsynced'> {
	try {
		await join();
		return 'synced';
	} catch (error) {
		if (!(error instanceof BeatSyncPhaseLockError)) throw error;
		log(`[beat-sync] deck ${deck} starts unsynced: ${error.message}`);
		onRefusal(error);
		await startUnsynced();
		return 'unsynced';
	}
}


// ----- PLAY-28: a master handover to a gridless deck mixes without Beat Sync -----
/*
 * Wed 7 Oct 2026 16:11:29Z, silver, packaged build 2411a8f23, Beat Sync Max on:
 * "auto-play: deck 1 master handover refused: Beat Sync: deck 1 requires a
 * valid real PQTZ beat grid: RangeError: beat grid must contain at least 2
 * beats, got 0". AutoPlay had started the incoming gridless track (PLAY-25),
 * then asked for master; `setDeckMaster` tried to re-lock the outgoing deck to
 * a master with no grid, the plain grid error was not a PLAY-27 plan refusal,
 * the master was reverted, and AutoPlay (which only arms off the master) never
 * queued another track: the set ran out into silence.
 *
 * rekordbox / Serato behaviour: a track with no grid can still be master; the
 * mix simply happens without Beat Sync. So the handover stands, the followers
 * play on at their own tempo, and the DJ is told so, honestly, once.
 *
 * Requirements (mini-PRD):
 *   ✔︎ A gridless new master stands and its followers play on unsynced.
 *     [if] setDeckMaster targets a playing deck with no trusted grid while a
 *       synced deck plays [then] the master moves and nothing throws
 *       [⛔️ if "master handover refused" comes back]
 *   ✔︎ A gridded new master still re-locks its followers.
 *     [if] the new master has a trusted grid [then] the normal sync runs
 *       [⛔️ if a valid grid is ignored and the mix silently goes unsynced]
 *   ✔︎ The fallback is visible, never silent.
 *     [if] the handover mixes without Beat Sync [then] a warn toast and a
 *       `beat-sync-gridless-master` perf event name the deck
 *       [⛔️ if the fallback happens with no notice]
 *   ✔︎ Later tempo and seek stay on free transport.
 *     [if] Beat Sync Max is on and the master has no trusted grid [then] a
 *       later pitch change or waveform seek does not call the beat-grid sync
 *       path [⛔️ if requireBeatGrid comes back until Beat Sync is turned off]
 *   ✔︎ A stale follower sync error does not outlive the fallback.
 *     [if] a follower had a sync_error [then] the unsynced mix clears it
 *       [⛔️ if the UI still shows that error]
 */

/** Perf-event kind recorded when a master handover mixes without Beat Sync. */
export const GRIDLESS_MASTER_PERF_KIND = 'beat-sync-gridless-master';

/** The notice the DJ sees when a gridless deck takes master over synced decks. */
export function gridlessMasterNotice(deck: number): string {
	return `Deck ${deck} has no beat grid: mixed without Beat Sync`;
}

/**
 * PLAY-28: must this master handover mix without Beat Sync?
 *
 * True only when there is someone to sync (`followerCount > 0`) AND the new
 * master has no trusted grid to sync them to. A gridded master always syncs.
 */
export function handoverMixesWithoutBeatSync(
	master: Pick<DeckState, 'anlz'>,
	followerCount: number
): boolean {
	return followerCount > 0 && !deckHasTrustedBeatGrid(master);
}

/**
 * The master later Beat Sync ownership and seek planning may use.
 *
 * A gridless master is still the master. It cannot drive a phase lock.
 * Callers that would pass the raw master into tempo ownership or seek
 * planning pass this instead and get null, so those controls take free
 * transport instead of `requireBeatGrid`. Playing is the caller's check:
 * a paused gridded master still owns tempo.
 */
export function beatSyncClockMaster<Id extends number>(
	master: Id | null,
	masterDeck: Pick<DeckState, 'anlz'> | null
): Id | null {
	if (master === null || masterDeck === null || !deckHasTrustedBeatGrid(masterDeck)) return null;
	return master;
}

/** PLAY-28: a gridless master stands; its would-be followers play on at their own tempo, and the DJ is told once. */
export function mixWithoutBeatSync(
	master: DeckState['deck_id'],
	followers: readonly DeckState['deck_id'][],
	pushToast: (message: string, kind: 'warn') => void,
	recordPerfEvent: (
		kind: string,
		detail: string,
		deck: DeckState['deck_id'],
		level: 'warn'
	) => void,
	clearSyncError: (deck: DeckState['deck_id']) => void
): void {
	for (const deck of followers) clearSyncError(deck);
	const notice = gridlessMasterNotice(master);
	console.info(`[beat-sync] ${notice} (decks ${followers.join(',')} play unsynced)`);
	pushToast(notice, 'warn');
	recordPerfEvent(GRIDLESS_MASTER_PERF_KIND, `${notice}; decks [${followers.join(',')}] play unsynced`, master, 'warn');
}

/**
 * DECKCTL-6235 (alpha status I11): where the deck AU button parks a load.
 * rekordbox's Auto Cue parks a loaded track at its first memory cue or memory
 * loop, else at the start of the music; Open DJ reads "start of the music" as the first
 * beat of the grid the deck draws, so a silent lead-in parks on beat 1 rather
 * than 0:00. With AU on, a finished track restarts from that cue too
 * (`autoCueRestartSec` in player/transport/schedule-math).
 */
export function autoCueTargetMs(
	anlz: Pick<AnlzData, 'cues' | 'beatgrid'> | null,
	durationMs: number | null
): number | null {
	if (anlz === null) return null;
	const limit = durationMs !== null && durationMs > 0 ? durationMs : Number.POSITIVE_INFINITY;
	let firstMemory: number | null = null;
	for (const cue of anlz.cues) {
		// A memory loop (rekordbox Kind 0 with an out point) is a memory entry too.
		if (cue.kind !== 'memory' && cue.kind !== 'loop') continue;
		if (!Number.isFinite(cue.in_ms) || cue.in_ms < 0 || cue.in_ms >= limit) continue;
		if (firstMemory === null || cue.in_ms < firstMemory) firstMemory = cue.in_ms;
	}
	if (firstMemory !== null) return firstMemory;
	const first = anlz.beatgrid.beats[0];
	if (first === undefined || !Number.isFinite(first.t) || first.t <= 0) return null;
	const ms = first.t * 1000;
	return ms < limit ? ms : null;
}
