/**
 * Beat Sync MASTER election (issue #320 / DECKUX-17).
 *
 * Pure policy: among playing loaded decks, prefer on-air, then Beat-Synced,
 * then loudest on-air gain, then lowest deck id. No engine or IPC imports.
 */
import { TRIM_MAX_GAIN } from '$lib/player/constants';
import type { DeckId } from '$lib/rb/deck-slots';
import type { CrossfaderAssign } from '$lib/rb/mixer-types';
import { nextPlayingMaster } from '$lib/rb/audio-engine-guards';
import type { MasterReason } from '$lib/rb/audio-engine-types';

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
	return deck.trim * TRIM_MAX_GAIN * deck.fader * _xfGainFor(deck.assign, crossfader) * master;
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

/** Automatic election runs under the dispatcher's all-deck plus sync claim. */
export type AutomaticMasterElectionRunner = (work: () => Promise<void>) => Promise<void>;
let automaticMasterElectionRunner: AutomaticMasterElectionRunner = (work) => work();
export function installAutomaticMasterElectionRunner(runner: AutomaticMasterElectionRunner): () => void {
 const previous = automaticMasterElectionRunner;
 automaticMasterElectionRunner = runner;
 return () => { automaticMasterElectionRunner = previous; };
}
export function runAutomaticMasterElection(work: () => Promise<void>): Promise<void> {
 return automaticMasterElectionRunner(work);
}
