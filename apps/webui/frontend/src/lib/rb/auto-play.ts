/**
 * Pure auto-play scheduling helpers for /performance v1.
 *
 * Trigger + deck/track selection + Beat Sync handoff policy. Engine mutations
 * go through performance-ipc (see auto-play.svelte.ts). No DSP / no mocks.
 *
 * =====================================================================
 * SCENARIO MATRIX - read this before changing any AutoPlay logic.
 * =====================================================================
 *
 * THE INVARIANT:
 *   Load a new track only when the playing master enters its ending window.
 *
 * So: exactly one load, per master track, when that master track is ending.
 * Never off a non-master deck, never twice, never the same track twice.
 *
 * Vocabulary
 *   source    the watched deck. Only ever is_master && playing && has a track.
 *   window    remainingMs(position, duration) <= effectiveAutoPlayThresholdMs
 *             = min(AUTO_PLAY_THRESHOLD_MS, duration/2), so a short track is
 *             never in-window at t=0.
 *   armed     a handoff was already committed for this source track.
 *   claimed   every stable_id AutoPlay has ever decided to load this session.
 *
 * #   State                                        Correct behavior
 * --  -------------------------------------------  ----------------------------
 * 1   No master / master paused / master empty      Nothing. Clear order. After
 *                                                   AUTO_PLAY_SILENT_STALL_MS (30s)
 *                                                   with no deck playing, raise a
 *                                                   no-deck-playing PLAY-08 stall.
 *                                                   After AUTO_PLAY_IDLE_DISARM_MS
 *                                                   (31s) with no pending master
 *                                                   promotion, disarm the pref too
 *                                                   and retain the stall banner.
 *                                                   Enabled with nothing playing:
 *                                                   armed-empty grace for
 *                                                   AUTO_PLAY_ARMED_EMPTY_DISARM_MS
 *                                                   (3min) with no silent stall.
 *                                                   The master gap between demote
 *                                                   and promote is NOT idle.
 * 2   Master playing, remaining > threshold         Nothing. Disarm this track only.
 * 3   Master playing, duration unknown (null)       Nothing. An unknown length must
 *                                                   never trigger a load.
 * 4   In window, not armed, follower free, next
 *     track available                               Commit (arm + claim), then load,
 *                                                   start, promote. Exactly one load.
 * 5   In window, already armed for this track       Nothing. Single-shot guarantee.
 * 6   Master looping inside the window              Nothing beyond the first arm.
 *                                                   The arm is keyed on the track and
 *                                                   is not rolled back, so a loop
 *                                                   cannot re-trigger.
 * 7   Master seeks back out, then plays to end      Disarm on leaving the window,
 *                                                   re-arm on re-entry. One per pass.
 * 8   Two decks playing, one is master              Only the master arms. See
 *                                                   pickSourceDeck.
 * 9   A deck is empty                               Prefer it as follower.
 * 10  No empty deck, one loaded-and-stopped         Use it. Never steal a playing deck.
 * 11  Every deck playing                            No load. Toast once. Do NOT arm -
 *                                                   retry when a deck frees.
 * 12  A handoff is already in flight                Nothing. One handoff at a time.
 * 13  Candidate already on another deck             Not pickable.
 * 14  Candidate already claimed earlier this
 *     session                                       Not pickable - even if that deck
 *                                                   was since unloaded and even if the
 *                                                   playlist feed epoch rolled.
 * 15  Playlist exhausted                            Toast, arm anyway (else it repeats
 *                                                   every poll), load nothing.
 * 16  Handoff fails BEFORE the track is on the
 *     deck (unload/load rejected)                   Nothing was committed. Quarantine
 *                                                   the candidate, re-arm, try another.
 *                                                   Bounded by MAX_HANDOFF_ATTEMPTS.
 * 17  Handoff fails AFTER the track is on the
 *     deck (beat_sync / play / master rejected)     The track IS on the deck. NEVER
 *                                                   re-arm, NEVER load anything else.
 * 18  Master promotion refused because the
 *     follower is not audible yet                   Not a handoff failure. Defer and
 *                                                   retry on later ticks once audible.
 * 19  Track shorter than 2x the constant window     Window clamps to duration/2, so
 *     (duration <= 32s)                             play never arms at t=0. Handoff
 *                                                   arms once half the track played.
 *                                                   A short track must not load
 *                                                   its successor over its intro.
 * 20  Feed observed EMPTY while enabled             Never snapshot an empty view -
 *     (pane not hydrated yet at activation)         keep tracking live, arm on the
 *                                                   first observation WITH rows.
 *                                                   An empty mount snapshot must
 *                                                   not freeze the whole session.
 * 21  Candidate is another library copy of a        Not pickable (PLAY-16): same title
 *     recording on a deck, played or claimed        once "N - " prefixes go, same lead
 *                                                   artist, so one song cannot sit
 *                                                   on several decks at once.
 *
 * CONVERGENCE - the simple logic all twenty rows reduce to:
 *   a. Source = the playing master, or nothing.                 (pickSourceDeck)
 *   b. Trigger = in the window AND not already armed.           (shouldTriggerAutoPlay)
 *   c. Arm and claim BEFORE dispatching anything, and treat both as one-way for
 *      the life of that source track.
 *   d. Exclusion = decks + played + claimed + quarantined, where `claimed` is
 *      monotonic for the session.                               (autoPlayExcludedIds)
 *      That one line is the entire no-duplicate guarantee.
 *   e. A handoff failure is retryable only before the load landed.
 *                                                        (handoffFailureIsRetryable)
 *   f. Master promotion is a separate idempotent deferred step, not the last
 *      await of the handoff.                                (decideMasterPromotion)
 *
 * (c)+(d) prevent duplicate track claims across decks; (e)+(f) prevent
 * a deferred master promotion from re-arming an already committed handoff.
 */
import type { AutoPlayTrackRow } from '$lib/rb/auto-play-chain';
import {
	pickNextStableId as pickCompatibleNextStableId,
	sameRecordingAsSpent
} from '$lib/rb/auto-play-chain';
export { createAutoPlayFeedSnapshot } from '$lib/rb/autoplay-feed';
import type { DeckId } from '$lib/rb/deck-slots';

/** Remaining presentation time that arms a handoff (~8 bars at 120 BPM). */
export const AUTO_PLAY_THRESHOLD_MS = 16_000;

/**
 * Trigger window for one track: min(AUTO_PLAY_THRESHOLD_MS, duration/2).
 *
 * Without the duration/2 clamp a track
 * shorter than the constant window is in-window at t=0, so pressing play
 * armed a handoff INSTANTLY and the next track started over it. Short
 * tracks (edits, stings) still hand off, but only after at least half of
 * them has played. Unknown duration returns null: an unknown length must
 * never trigger a load (scenario row 3).
 */
export function effectiveAutoPlayThresholdMs(duration_ms: number | null): number | null {
	if (duration_ms === null || !Number.isFinite(duration_ms) || duration_ms <= 0) return null;
	return Math.min(AUTO_PLAY_THRESHOLD_MS, Math.floor(duration_ms / 2));
}

/** Follower Beat Sync policy after AutoPlay load, before play. */
export type AutoPlayBeatSyncDecision = 'enable' | 'disable';

export type AutoPlayDeckSnap = {
	id: DeckId;
	stable_id: string | null;
	playing: boolean;
	position_ms: number;
	duration_ms: number | null;
	is_master: boolean;
	beat_sync_enabled: boolean;
};

// ----- remaining / trigger ------------------------------------------------

/** Presentation remaining ms; null when duration unknown. */
export function remainingMs(position_ms: number, duration_ms: number | null): number | null {
	if (duration_ms === null || !Number.isFinite(duration_ms) || duration_ms <= 0) return null;
	if (!Number.isFinite(position_ms) || position_ms < 0) return null;
	return Math.max(0, duration_ms - position_ms);
}

/**
 * True once per outgoing track when remaining enters the threshold window.
 * Resets when `alreadyTriggeredFor` no longer matches the source stable id
 * (new track / unload) or when remaining leaves the window via seek.
 */
export function shouldTriggerAutoPlay(input: {
	enabled: boolean;
	remaining_ms: number | null;
	threshold_ms: number;
	source_stable_id: string | null;
	already_triggered_for: string | null;
	in_flight: boolean;
}): boolean {
	if (!input.enabled || input.in_flight) return false;
	if (input.source_stable_id === null || input.remaining_ms === null) return false;
	if (input.remaining_ms > input.threshold_ms) return false;
	if (input.already_triggered_for === input.source_stable_id) return false;
	return true;
}

// ----- deck pick ----------------------------------------------------------

/**
 * Master only - never a non-master playing deck. With multiple decks
 * playing (any DJ mix), arming a handoff off every one of them independently
 * staggers new loads forever (two decks ending 1m apart => new tracks load
 * 1m apart, indefinitely). AutoPlay follows the master; the operator (or
 * AutoPlay's own handoff, see _handoff) decides what master is.
 */
export function pickSourceDeck(decks: readonly AutoPlayDeckSnap[]): AutoPlayDeckSnap | null {
	return decks.find((d) => d.is_master && d.playing && d.stable_id !== null) ?? null;
}

/**
 * Free (empty) non-source deck first, else stopped (loaded, not playing)
 * non-source. Never steals a playing deck.
 */
export function pickFollowerDeck(
	decks: readonly AutoPlayDeckSnap[],
	sourceId: DeckId
): DeckId | null {
	const others = decks.filter((d) => d.id !== sourceId);
	const empty = others.find((d) => d.stable_id === null);
	if (empty !== undefined) return empty.id;
	const stopped = others.find((d) => d.stable_id !== null && !d.playing);
	return stopped?.id ?? null;
}

// ----- handoff commit point (scenarios 16, 17) ---------------------------

/**
 * How far a handoff attempt got before it threw.
 *
 * 'load'   nothing has been committed to the follower yet (the unload/load
 *          dispatch itself failed), so the deck is untouched.
 * 'commit' the track IS on the follower deck; a later step (Beat Sync, play,
 *          master promotion) failed.
 */
export type AutoPlayHandoffPhase = 'load' | 'commit';

/**
 * May AutoPlay re-arm and pick a different candidate after a failed handoff?
 *
 * Only while nothing has landed on the deck. Re-arming after the track is
 * already loaded is what produced the live duplicate: the master promotion
 * threw, the catch reset the trigger, and 250 ms later AutoPlay loaded a
 * second track onto a second deck. After 'commit' the operator has a loaded, started deck - that
 * handoff is spent whether or not every optional step succeeded.
 */
export function handoffFailureIsRetryable(phase: AutoPlayHandoffPhase): boolean {
	if (phase === 'load') return true;
	if (phase === 'commit') return false;
	const _exhaustive: never = phase;
	throw new Error(`unhandled handoff phase: ${String(_exhaustive)}`);
}

// ----- exclusion set (scenarios 13, 14) -----------------------------------

/**
 * Every stable_id AutoPlay must refuse to pick.
 *
 * Union of: tracks sitting on a non-source deck, tracks AutoPlay already
 * claimed this session, and quarantined candidates. The source deck's own
 * track is deliberately absent - pickNextStableId excludes it by identity.
 *
 * `claimed_ids` is the no-duplicate guarantee and is the reason it is separate
 * from the deck scan: the deck scan only sees where a track is RIGHT NOW, so
 * it misses a track that was loaded and then unloaded, or one whose load has
 * not published its stable_id yet. A claim is recorded at decision time and is
 * never rolled back, so an id AutoPlay has once decided to load can never be
 * decided again.
 */
export function autoPlayExcludedIds(input: {
	decks: readonly AutoPlayDeckSnap[];
	source_deck: DeckId;
	claimed_ids: ReadonlySet<string>;
	unplayable_ids: ReadonlySet<string>;
}): Set<string> {
	const out = new Set<string>();
	for (const d of input.decks) {
		if (d.id === input.source_deck) continue;
		if (d.stable_id !== null) out.add(d.stable_id);
	}
	for (const id of input.claimed_ids) out.add(id);
	for (const id of input.unplayable_ids) out.add(id);
	return out;
}

// ----- deferred master promotion (scenario 18) ---------------------------

/**
 * 'promote' dispatch master now; 'wait' retry on a later tick; 'drop' forget it.
 */
export type AutoPlayMasterPromotion = 'promote' | 'wait' | 'drop';

/**
 * Whether AutoPlay may hand master to the deck it just started.
 *
 * engine.setDeckMaster refuses a deck that is not yet audible while any other
 * deck is ("setDeckMaster: cannot select paused deck N while decks [...] are
 * audible or scheduled to play"). AutoPlay used to ask immediately after
 * `play` resolved - but `play` only SCHEDULES the transport, and `audible` is
 * published later by the presented-transport observation. The outgoing master
 * is still audible at that moment by definition, so the ask was refused and
 * the whole handoff was recorded as failed.
 *
 * Asking only once the follower is actually presenting audio removes the race
 * instead of catching it. Waiting is bounded in practice by the follower
 * becoming audible; if the operator takes the deck over in the meantime the
 * promotion is dropped rather than forced.
 */
export function decideMasterPromotion(input: {
	pending_deck: DeckId | null;
	pending_stable_id: string | null;
	deck: AutoPlayDeckSnap | null;
	audible: boolean;
}): AutoPlayMasterPromotion {
	if (input.pending_deck === null || input.pending_stable_id === null) return 'drop';
	if (input.deck === null || input.deck.id !== input.pending_deck) return 'drop';
	// Operator loaded/unloaded something else there: their deck, their call.
	if (input.deck.stable_id !== input.pending_stable_id) return 'drop';
	if (input.deck.is_master) return 'drop';
	if (!input.deck.playing) return 'drop';
	if (!input.audible) return 'wait';
	return 'promote';
}

// ----- chain / pick algebra (auto-play-chain.ts) --------------------------
// Re-exported rather than moved-and-rewired, so every consumer keeps importing
// what it always imported and no other file changes.
//
// ONLY the symbols with a real static importer are re-exported. knip runs with
// ignoreExportsUsedInFile, so a symbol re-exported here that nobody imports
// FROM here counts as an unreferenced export even though it is very much used
// inside auto-play-chain.ts. AUTO_PLAY_REACH_MAX_STEPS, pickNextMaximizingReach
// and ReachPick are exactly that case - their only outside consumer is a unit
// test loading the module by path - so they are imported straight from
// auto-play-chain.ts instead of being widened into a surface here.
//
// Deliberately NOT a two-way barrel: this file imports from auto-play-chain.ts
// and never the reverse, so no import cycle is introduced
// (frontend.import_cycles allows 1 and is already spent).
export { bpmWithinPhaseLockRange, simulateAutoPlayChain } from '$lib/rb/auto-play-chain';
export type { AutoPlayTrackRow } from '$lib/rb/auto-play-chain';

/** One exact handoff in the user-viewable AutoPlay queue. */
export type AutoPlayQueueEntry = {
	stable_id: string;
	title: string | null;
	artist: string | null;
};

/**
 * Resolve planned handoffs against the frozen activation feed.
 *
 * The queue must never consult the browser's live view: a re-sort after
 * activation is intentionally invisible to AutoPlay until it is toggled off
 * and on again (PLAY-04). A missing row is an invariant failure, not an
 * omission we can hide, because playback and the displayed queue would diverge.
 */
export function queueEntriesForChain(
	chain: readonly string[],
	playlist: readonly AutoPlayTrackRow[]
): readonly AutoPlayQueueEntry[] {
	const rowsById = new Map(playlist.map((row) => [row.stable_id, row] as const));
	return chain.map((stableId) => {
		const row = rowsById.get(stableId);
		if (row === undefined) {
			throw new Error(`autoplay queue: planned stable_id ${stableId} is absent from frozen feed`);
		}
		return { stable_id: stableId, title: row.title ?? null, artist: row.artist ?? null };
	});
}

/**
 * Pick the best compatible next row, then keep the set moving with the first
 * remaining playable playlist row when compatibility candidates are spent.
 *
 * The fallback deliberately preserves playlist order and all no-repeat
 * exclusions. It is a last resort, not a replacement for the optimized plan.
 */
export function pickNextStableId(input: Parameters<typeof pickCompatibleNextStableId>[0]): string | null {
	const compatible = pickCompatibleNextStableId(input);
	if (compatible !== null || input.enforce_play_order) return compatible;
	const repeatsARecording = sameRecordingAsSpent(input);
	for (const row of input.playlist) {
		if (
			row.stable_id !== input.current_stable_id &&
			row.file_exists &&
			!input.exclude_ids.has(row.stable_id) &&
			!input.played_ids.has(row.stable_id) &&
			!repeatsARecording(row)
		) {
			return row.stable_id;
		}
	}
	return null;
}

/** Ascending chart rank, retaining source order for equal and unranked rows. */
function compareAutoPlayOrder(
	stableIdA: string,
	stableIdB: string,
	rankOf: ReadonlyMap<string, number>
): number {
	const rankA = rankOf.get(stableIdA) ?? null;
	const rankB = rankOf.get(stableIdB) ?? null;
	if (rankA === rankB) return 0;
	else if (rankA === null) return 1;
	else if (rankB === null) return -1;
	return rankA - rankB;
}

/** Return a new AutoPlay-ranked view without mutating stored membership order. */
export function sortRowsByAutoPlayOrder<T extends { stable_id: string }>(
	rows: readonly T[],
	rankOf: ReadonlyMap<string, number>
): T[] {
	return rows.slice().sort((a, b) => compareAutoPlayOrder(a.stable_id, b.stable_id, rankOf));
}

// ----- browser feed (playlist membership + key/BPM) -----------------------

let _playlist: AutoPlayTrackRow[] = [];
let _feedEpoch = 0;
let _playlistIdentity = '';
/** Selector-input revision. Unlike membership epoch, metadata edits advance it. */
let _playlistRevision = 0;
let _playlistSelectorIdentity = '';
let _playlistScope: string | null | undefined;

const EMPTY_AUTOPLAY_RANKS: ReadonlyMap<string, number> = new Map();
let _rankProvider: (() => ReadonlyMap<string, number>) | null = null;

/** Install the controller's reactive chart rank reader for BrowserPanel. */
export function registerAutoPlayRankProvider(
	provider: () => ReadonlyMap<string, number>
): () => void {
	if (_rankProvider !== null) throw new Error('AutoPlay rank provider already installed');
	_rankProvider = provider;
	return () => {
		if (_rankProvider === provider) _rankProvider = null;
	};
}

/** Empty until the AutoPlay controller lifecycle is installed. */
export function getAutoPlayRankOf(): ReadonlyMap<string, number> {
	return _rankProvider?.() ?? EMPTY_AUTOPLAY_RANKS;
}

/** Browser publishes open-playlist membership order with key/BPM/file_exists. */
export function setAutoPlayTrackFeed(
	playlistScope: string | null,
	playlist: readonly AutoPlayTrackRow[]
): void {
	_playlist = playlist.map((r) => ({
		stable_id: r.stable_id,
		key: r.key,
		bpm: r.bpm,
		file_exists: r.file_exists === true,
		...(r.title === undefined ? {} : { title: r.title }),
		...(r.artist === undefined ? {} : { artist: r.artist })
	}));
	// Scope is part of feed identity because distinct playlists may contain the
	// same stable IDs in the same order. Rating/key edits within one playlist
	// must not wipe AutoPlay played history mid-set.
	const identity = _playlist.map((r) => r.stable_id).join('\0');
	if (playlistScope !== _playlistScope || identity !== _playlistIdentity) {
		_playlistScope = playlistScope;
		_playlistIdentity = identity;
		_feedEpoch += 1;
	}
	const selectorIdentity = _playlist
		.map((r) => [r.stable_id, r.key, r.bpm, r.file_exists ? 1 : 0].join('\0'))
		.join('\x01');
	if (selectorIdentity !== _playlistSelectorIdentity) {
		_playlistSelectorIdentity = selectorIdentity;
		_playlistRevision += 1;
	}
}

/**
 * Rows still available to the picker: not the source, not on a deck, not played.
 *
 * Pure, and separate from the controller, because it is what the PLAY-08 stall
 * descriptor names when AutoPlay gives up - the operator has to be able to read
 * WHICH tracks stopped the set, and a list derived inline in a 250 ms poll is
 * one nobody can test.
 */
export function remainingAutoPlayCandidates(input: {
	playlist: readonly AutoPlayTrackRow[];
	current_stable_id: string;
	exclude_ids: ReadonlySet<string>;
	played_ids: ReadonlySet<string>;
}): readonly AutoPlayTrackRow[] {
	return input.playlist.filter(
		(row) =>
			row.stable_id !== input.current_stable_id &&
			!input.exclude_ids.has(row.stable_id) &&
			!input.played_ids.has(row.stable_id)
	);
}

export function getAutoPlayPlaylist(): readonly AutoPlayTrackRow[] {
	return _playlist;
}

/** @deprecated use getAutoPlayPlaylist - kept for transitional callers/tests */
export function getAutoPlayPlaylistIds(): readonly string[] {
	return _playlist.map((r) => r.stable_id);
}

export function getAutoPlayFeedEpoch(): number {
	return _feedEpoch;
}

/** Changes whenever a playlist field used by the AutoPlay picker changes. */
export function getAutoPlayPlaylistRevision(): number {
	return _playlistRevision;
}

// ----- Beat Sync handoff policy -------------------------------------------

/** Pitch-range percent -> closed tempo-ratio bounds (matches audio-engine). */
export function tempoBoundsFromPitchRange(pitchRangePct: number): { min: number; max: number } {
	if (!Number.isFinite(pitchRangePct) || pitchRangePct < 0) {
		throw new RangeError(`pitchRangePct must be a finite non-negative number, got ${pitchRangePct}`);
	}
	const range = pitchRangePct / 100;
	return { min: Math.max(0.01, 1 - range), max: 1 + range };
}

/**
 * Decide follower Beat Sync for AutoPlay handoff.
 *
 * BAR/BEAT lock math stays in beat-sync-math; AutoPlay only chooses whether
 * to request sync. Never auto-switches to BEAT mode.
 */
export function decideAutoPlayBeatSync(input: {
	source_beat_sync_enabled: boolean;
	phase_lock_ok: boolean;
}): AutoPlayBeatSyncDecision {
	if (input.source_beat_sync_enabled && input.phase_lock_ok) return 'enable';
	return 'disable';
}

/** Operator-facing toast when AutoPlay skips Beat Sync after a failed plan. */
export function formatAutoPlaySyncSkipToast(input: {
	follower_deck: DeckId;
	mode: string;
	plan_error: string;
	min_ratio: number;
	max_ratio: number;
}): string {
	// One tip, because BAR now has exactly one way to refuse. It used to have
	// two: a plan needing 0.5x/2x threw a message carrying
	// `tempoNormalization=...`, and this branched on that substring to say
	// "Select BEAT mode for half/double tempo matching". BAR now FOLDS
	// instead of throwing there, so `computeFollowerSyncPlan`'s only
	// remaining BAR throw is the pitch-range-exhausted message - which never
	// contains that substring. The branch was dead, and its test proved
	// nothing because it hand-built the impossible error string rather than
	// deriving one.
	const tip =
		` BAR needs a twin within pitch range [${input.min_ratio}, ${input.max_ratio}]; Beat Sync left off.`;
	return (
		`auto-play: deck ${input.follower_deck} Beat Sync skipped (${input.mode}): ` +
		`${input.plan_error}.${tip}`
	);
}

/**
 * Identity of everything the charted AutoPlay order depends on. The 250 ms poll
 * must not re-simulate an unchanged chain over the open playlist on every tick,
 * so the controller recomputes only when this key changes. Private library
 * census and timing measurements are not included in the public source.
 */
export function chartedOrderKey(input: {
	feed_epoch: number;
	playlist_revision: number;
	source_stable_id: string;
	source_key: string | null;
	source_bpm: number | null;
	enforce_play_order: boolean;
	maximize_reach: boolean;
	min_tempo_ratio: number;
	max_tempo_ratio: number;
	exclude_ids: ReadonlySet<string>;
	played_ids: ReadonlySet<string>;
	follower_deck: DeckId;
	follower_pitch_range: number;
}): string {
	const ids = (s: ReadonlySet<string>) => [...s].sort().join(',');
	return [
		input.feed_epoch,
		input.playlist_revision,
		input.source_stable_id,
		input.source_key,
		input.source_bpm,
		input.enforce_play_order ? 1 : 0,
		input.maximize_reach ? 1 : 0,
		input.min_tempo_ratio,
		input.max_tempo_ratio,
		ids(input.exclude_ids),
		ids(input.played_ids),
		input.follower_deck,
		input.follower_pitch_range
	].join('|');
}
