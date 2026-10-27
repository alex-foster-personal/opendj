/**
 * Beat Sync MASTER election (issue #320 / DECKUX-17).
 *
 * Election policy: among playing loaded decks, prefer on-air, then Beat-Synced,
 * then loudest on-air gain, then lowest deck id. The dispatcher installs the
 * shared automatic handoff runner here. No engine or IPC imports.
 */
import { TRIM_MAX_GAIN } from '$lib/player/constants';
import { autoGainLinear } from '$lib/rb/auto-gain.svelte';
import type { DeckId } from '$lib/rb/deck-slots';
import type { CrossfaderAssign } from '$lib/rb/mixer-types';
import { nextPlayingMaster } from '$lib/rb/audio-engine-guards';
import type { MasterReason } from '$lib/rb/audio-engine-types';
import { externallyRoutedDecks } from '$lib/sets/deck-audibility';
import {
	heardLevelDb,
	stepLevelHandoff,
	type HeardState,
	type LevelHandoffDeck,
	type LevelHandoffState,
	type LevelHandoffStep
} from './master-handoff';

export {
	HANDOFF_TICK_MS,
	LEVEL_HANDOFF_IDLE,
	masterHandoffMessage,
	stepLevelHandoff,
	type LevelHandoffState,
	type LevelHandoffStep
} from './master-handoff';

export const SILENCE_GAIN_EPSILON = 1e-4;

/**
 * Election reasons that move the master AWAY from a deck the followers were
 * locked to without the user picking the new one: the master paused, faded
 * out, played out or was unloaded. The engine re-joins the playing Beat Sync
 * followers to the new master after one of these (NAE-19), as it does after a
 * manual switch. Claims, Beat Sync enable and unlock re-elections are not
 * handoffs: their callers already join the deck that asked.
 */
export const AUTOMATIC_HANDOFF_REASONS: ReadonlySet<MasterReason> = new Set<MasterReason>([
	'master-left',
	'natural-end',
	'unload'
]);

export interface MasterElectionDeck {
	id: DeckId;
	loaded: boolean;
	playing: boolean;
	beat_sync_enabled: boolean;
	fader: number;
	trim: number;
	assign: CrossfaderAssign;
}

export interface MasterElectionInput {
	decks: readonly MasterElectionDeck[];
	crossfader: number;
	master: number;
}

/** Equal-power crossfade gain for one bus assignment at position x (0..1). */
function _xfGainFor(assign: CrossfaderAssign, x: number): number {
	if (assign === 'THRU') return 1;
	if (assign === 'A') return Math.cos((x * Math.PI) / 2);
	return Math.cos(((1 - x) * Math.PI) / 2);
}

export function onAirGain(deck: MasterElectionDeck, crossfader: number, master: number): number {
	// `trim` is the knob; the trim stage also multiplies in the deck's auto
	// gain (AUTOGAIN-02, up to 12 dB either way), read here so every caller
	// (Web Audio election and handoff, the Rust sync) judges what is heard.
	return deck.trim * TRIM_MAX_GAIN * autoGainLinear(deck.id) * deck.fader * _xfGainFor(deck.assign, crossfader) * master;
}

function _isEligible(deck: MasterElectionDeck): boolean {
	return deck.loaded && deck.playing;
}

function _compareCandidates(
	a: MasterElectionDeck,
	b: MasterElectionDeck,
	crossfader: number,
	master: number
): number {
	const aGain = onAirGain(a, crossfader, master);
	const bGain = onAirGain(b, crossfader, master);
	const aOnAir = aGain >= SILENCE_GAIN_EPSILON ? 1 : 0;
	const bOnAir = bGain >= SILENCE_GAIN_EPSILON ? 1 : 0;
	if (aOnAir !== bOnAir) return bOnAir - aOnAir;
	if (a.beat_sync_enabled !== b.beat_sync_enabled) {
		return (b.beat_sync_enabled ? 1 : 0) - (a.beat_sync_enabled ? 1 : 0);
	}
	if (aGain !== bGain) return bGain - aGain;
	return a.id - b.id;
}

/** Elect the Beat Sync MASTER from eligible playing decks, or null if none. */
export function electMaster(input: MasterElectionInput): DeckId | null {
	const eligible = input.decks.filter(_isEligible);
	if (eligible.length === 0) return null;
	if (eligible.length === 1) return eligible[0]!.id;
	const sorted = [...eligible].sort((a, b) =>
		_compareCandidates(a, b, input.crossfader, input.master)
	);
	return sorted[0]!.id;
}

/** Lowest deck id among eligible playing decks (last tie-break helper). */
export function lowestPlayingMaster(input: MasterElectionInput): DeckId | null {
	const playingIds = input.decks.filter(_isEligible).map((deck) => deck.id);
	return nextPlayingMaster(playingIds);
}

/** Supersedes the separate automatic-rejoin.ts runner: master election owns handoffs. */
/** Runs an automatic master handoff's follower re-join. The dispatcher installs
 * one that takes every deck's scope plus 'sync' (installed rather than
 * imported, since this module is imported FROM there); until then it runs now. */
export type AutomaticRejoinRunner = (work: () => Promise<void>) => Promise<void>;
let _automaticRejoinRunner: AutomaticRejoinRunner = (work) => work();

export function installAutomaticRejoinRunner(runner: AutomaticRejoinRunner): () => void {
	const previous = _automaticRejoinRunner;
	_automaticRejoinRunner = runner;
	return () => {
		_automaticRejoinRunner = previous;
	};
}

export function runAutomaticRejoin(work: () => Promise<void>): Promise<void> {
	return _automaticRejoinRunner(work);
}

/** Automatic election runs under the dispatcher's all-deck plus sync claim. */
export type AutomaticMasterElectionRunner = (work: () => Promise<void>) => Promise<void>;
let automaticMasterElectionRunner: AutomaticMasterElectionRunner = (work) => work();

export function installAutomaticMasterElectionRunner(runner: AutomaticMasterElectionRunner): () => void {
	const previous = automaticMasterElectionRunner;
	automaticMasterElectionRunner = runner;
	return () => {
		automaticMasterElectionRunner = previous;
	};
}

export function runAutomaticMasterElection(work: () => Promise<void>): Promise<void> {
	return automaticMasterElectionRunner(work);
}

/** The engine's live decks and mixer, read for a level handoff: each deck's
 * playing flag and its heard level after EQ, filter and stems
 * (master-handoff.ts). `routed` is the `?extroute=` set, read by the caller at
 * the browser boundary, as the graph reads it. */
export function levelHandoffDecks(
	state: HeardState & { decks: Record<DeckId, { playing: boolean; stable_id: string | null }> },
	ids: readonly DeckId[],
	masterMuted: boolean,
	routed: ReadonlySet<DeckId>
): LevelHandoffDeck[] {
	return ids.map((id) => ({
		id,
		playing: state.decks[id].playing && state.decks[id].stable_id !== null,
		levelDb: heardLevelDb(state, id, masterMuted, routed)
	}));
}

/** One step of the level hold from the engine's live decks and mixer: the
 * master's tempo sets the one-beat hold, levels are heard levels. */
export function stepLiveLevelHandoff(
	prev: LevelHandoffState,
	live: {
		master: DeckId | null;
		locked: boolean;
		state: HeardState & { decks: Record<DeckId, { playing: boolean; stable_id: string | null; bpm: number | null; pitch: number }> };
		masterMuted: boolean;
		/** The page's URL query string, read by the caller at the browser boundary. */
		extrouteQuery: string;
	},
	ids: readonly DeckId[],
	nowMs: number
): LevelHandoffStep {
	const m = live.master === null ? null : live.state.decks[live.master];
	const masterBpm = m === null || m.bpm === null ? null : m.bpm * m.pitch;
	const decks = levelHandoffDecks(live.state, ids, live.masterMuted, externallyRoutedDecks(live.extrouteQuery));
	return stepLevelHandoff(prev, { master: live.master, locked: live.locked, masterBpm, decks, nowMs });
}

/** A queued level handoff that failed is surfaced, never swallowed: an error
 * toast for the DJ plus an error-level performance event (escalated). */
export function reportLevelHandoffFailure(
	master: DeckId,
	error: unknown,
	record: (kind: string, message: string, deck: DeckId, level: 'error') => void,
	toast: (message: string, level: 'error') => void
): void {
	record('master-handoff-failed', `level handoff from ${master}: ${String(error)}`, master, 'error');
	toast(`MASTER handoff from deck ${master} failed: ${error instanceof Error ? error.message : String(error)}`, 'error');
}

/** What a MASTER button press sends (the maintainer, Tue 27 Oct 2026). A click PICKS the
 * deck and leaves AUTO handoff on; only the lock gesture (long-press, or a
 * Shift/Alt click) locks, and the same gesture on the locked master unlocks. */
export function masterButtonCommand(
	current: { master_deck: DeckId | null; master_mode: 'auto' | 'locked' },
	deck: DeckId,
	lockGesture: boolean
): { type: 'master'; deck: DeckId; lock: boolean } {
	if (!lockGesture) return { type: 'master', deck, lock: false };
	const lockedHere = current.master_deck === deck && current.master_mode === 'locked';
	return { type: 'master', deck, lock: !lockedHere };
}

/** Hold this long on MASTER to lock it. */
export const MASTER_LOCK_LONG_PRESS_MS = 500;
