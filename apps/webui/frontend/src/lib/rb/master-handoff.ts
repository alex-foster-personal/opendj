/**
 * Automatic MASTER handoff by what the room hears (the maintainer, Tue 27 Oct 2026).
 *
 * The old rule handed off only when the master's mixer gain reached exact
 * silence (< 1e-4) and read trim, fader, crossfader and master gain alone, so
 * a master killed on EQ, swept out on the filter or with every stem muted
 * stayed master, and so did one faded to a whisper. This module estimates the
 * level each deck reaches the speakers at, AFTER EQ, filter and stems, and
 * decides with a hold and two thresholds:
 *
 *   hand off when the master stays below MASTER_QUIET_DB for one beat at its
 *   own tempo (HANDOFF_HOLD_FALLBACK_MS when it has no tempo) while another
 *   PLAYING deck is above CANDIDATE_LOUD_DB; the loudest such deck wins.
 *
 * The level is a gain-chain ESTIMATE, not a meter: it says what the mixer
 * lets through at unity programme, so a breakdown in the music never moves
 * the master. Stages, each from the function that drives the real nodes:
 *   - path gain: deck-audibility.ts `masterPathGain` (trim, fader, crossfader
 *     with THRU exempt as the graph wires it, master gain, `?extroute=`);
 *   - transport not presenting audio, or the master mute: silent;
 *   - stems: RMS of stem-graph.ts `stemPartGains` (mute, solo, layout);
 *   - EQ: power mean of the three bands, a band at full cut counted as killed
 *     (`stemLinearFromKnob`, the stem-mode law; the plain shelf leaves -26 dB,
 *     which nobody hears under a deck at -20 dB);
 *   - filter: the share of the 20 Hz to 20 kHz octaves the LP/HP still passes.
 *
 * Pure: no runtime, no stores, no timers. The engine calls `stepLevelHandoff`
 * on every mixer change, play/pause and a 100 ms tick, and owns the clock.
 */
import {
	FILTER_HP_FLOOR_HZ,
	FILTER_LP_CEILING_HZ,
	stemLinearFromKnob
} from '$lib/player/constants';
import { filterParamsFromKnob } from '$lib/rb/audio-engine-guards';
import type { DeckId } from '$lib/rb/deck-id';
import { stemPartGains } from '$lib/rb/stem-graph';
import { type everyStemPartSilent, masterPathGain } from '$lib/sets/deck-audibility';

/** The deck fields and mixer the level reads, typed off deck-audibility's own inputs. */
type DeckStems = Parameters<typeof everyStemPartSilent>[0];
type Mixer = Parameters<typeof masterPathGain>[0]['mixer'];

export const MASTER_QUIET_DB = -30;
export const CANDIDATE_LOUD_DB = -20;
/** Hold when the master has no tempo to count one beat by. */
export const HANDOFF_HOLD_FALLBACK_MS = 500;
/** How often the engine re-checks without a mixer or transport event. */
export const HANDOFF_TICK_MS = 100;

const SPECTRUM_LOW_HZ = 20;
const SPECTRUM_OCTAVES = Math.log2(20000 / SPECTRUM_LOW_HZ);

function _octaveShareBelow(hz: number): number {
	return Math.min(1, Math.max(0, Math.log2(hz / SPECTRUM_LOW_HZ) / SPECTRUM_OCTAVES));
}

/** Power share of a broadband signal the filter knob still passes. */
export function filterPowerShare(knob: number): number {
	const p = filterParamsFromKnob(knob);
	const lp = p.lpHz >= FILTER_LP_CEILING_HZ ? 1 : _octaveShareBelow(p.lpHz);
	const hp = p.hpHz <= FILTER_HP_FLOOR_HZ ? 1 : 1 - _octaveShareBelow(p.hpHz);
	return p.dryGain ** 2 + p.lpWetGain ** 2 * lp + p.hpWetGain ** 2 * hp;
}

/** Power share of a broadband signal the three EQ bands pass, equal thirds. */
export function eqPowerShare(low: number, mid: number, high: number): number {
	return (stemLinearFromKnob(low) ** 2 + stemLinearFromKnob(mid) ** 2 + stemLinearFromKnob(high) ** 2) / 3;
}

function _stemPowerShare(deck: DeckStems): number {
	const stems = deck.stems;
	if (stems.status !== 'ready') return 1; // not separated yet: the full mix plays
	if (stems.layout === null) throw new Error('stems are ready but have no layout: the stem-state contract is broken');
	const gains = Object.values(stemPartGains(stems.controls, stems.layout));
	if (gains.length === 0) throw new Error('stems are ready but the layout has no parts: the stem-state contract is broken');
	return gains.reduce((sum, g) => sum + g * g, 0) / gains.length;
}

/** What the level estimate reads: each deck's transport flag and stems, and the mixer. */
export interface HeardState {
	decks: Record<DeckId, DeckStems & { audible: boolean }>;
	mixer: Mixer;
}

/** Estimated level, in dB relative to unity programme, that this deck
 * reaches the speakers at. -Infinity when nothing of it is heard. */
export function heardLevelDb(
	state: HeardState,
	deckId: DeckId,
	masterMuted: boolean,
	routedDecks: ReadonlySet<DeckId>
): number {
	const deck = state.decks[deckId];
	if (!deck.audible || masterMuted) return -Infinity;
	const ch = state.mixer.channels[deckId];
	const power =
		masterPathGain(state, deckId, routedDecks) ** 2 *
		eqPowerShare(ch.eq_low, ch.eq_mid, ch.eq_high) *
		filterPowerShare(ch.filter) *
		_stemPowerShare(deck);
	return power <= 0 ? -Infinity : 10 * Math.log10(power);
}

/** One beat at `bpm`, or the fallback when there is no usable tempo. */
export function handoffHoldMs(bpm: number | null): number {
	if (bpm === null) return HANDOFF_HOLD_FALLBACK_MS;
	if (!Number.isFinite(bpm) || bpm <= 0) throw new Error(`master tempo is not a BPM: ${bpm}`);
	return 60000 / bpm;
}

export interface LevelHandoffDeck {
	id: DeckId;
	playing: boolean;
	levelDb: number;
}

export interface LevelHandoffInput {
	master: DeckId | null;
	/** Locked masters never move on level; stop, end and unload still move them. */
	locked: boolean;
	/** The master's current tempo, for the one-beat hold. */
	masterBpm: number | null;
	decks: readonly LevelHandoffDeck[];
	nowMs: number;
}

/** When the current master first went quiet with a loud candidate waiting. */
export interface LevelHandoffState {
	master: DeckId | null;
	quietSinceMs: number | null;
}

export const LEVEL_HANDOFF_IDLE: LevelHandoffState = { master: null, quietSinceMs: null };

export interface LevelHandoffStep {
	state: LevelHandoffState;
	handoffTo: DeckId | null;
	masterDb: number;
	toDb: number | null;
}

function _loudestCandidate(input: LevelHandoffInput): LevelHandoffDeck | null {
	let best: LevelHandoffDeck | null = null;
	for (const deck of input.decks) {
		if (deck.id === input.master || !deck.playing || !(deck.levelDb > CANDIDATE_LOUD_DB)) continue;
		if (best === null || deck.levelDb > best.levelDb || (deck.levelDb === best.levelDb && deck.id < best.id)) {
			best = deck;
		}
	}
	return best;
}

/**
 * Advance the hold. The quiet clock starts when the master drops below
 * MASTER_QUIET_DB WHILE a loud candidate is playing, and resets the moment
 * either stops being true, so a dip that comes back never hands off.
 */
export function stepLevelHandoff(prev: LevelHandoffState, input: LevelHandoffInput): LevelHandoffStep {
	const masterDeck = input.decks.find((d) => d.id === input.master);
	const masterDb = masterDeck?.levelDb ?? -Infinity;
	const idle = (): LevelHandoffStep => ({
		state: { master: input.master, quietSinceMs: null },
		handoffTo: null,
		masterDb,
		toDb: null
	});
	if (input.master === null || input.locked || masterDeck === undefined || !masterDeck.playing) return idle();
	if (!(masterDb < MASTER_QUIET_DB)) return idle();
	const candidate = _loudestCandidate(input);
	if (candidate === null) return idle();
	const since = prev.master === input.master && prev.quietSinceMs !== null ? prev.quietSinceMs : input.nowMs;
	if (input.nowMs - since < handoffHoldMs(input.masterBpm)) {
		return { state: { master: input.master, quietSinceMs: since }, handoffTo: null, masterDb, toDb: candidate.levelDb };
	}
	// The quiet clock is kept after a decision: a handoff queued behind a command is
	// decided again by this same step when its turn comes, and only an unbroken dip
	// still has a full beat behind it then (a recovery resets the clock to null).
	return { state: { master: input.master, quietSinceMs: since }, handoffTo: candidate.id, masterDb, toDb: candidate.levelDb };
}

function _fmtDb(db: number | null): string {
	if (db === null || db === -Infinity) return '-inf';
	if (!Number.isFinite(db)) throw new Error(`level is not a dB value: ${db}`);
	return db.toFixed(1);
}

/** The performance-event line every master change writes. */
export function masterHandoffMessage(
	from: DeckId | null,
	to: DeckId | null,
	reason: string,
	levels?: { masterDb: number; toDb: number | null }
): string {
	const base = `master ${from ?? 'none'} -> ${to ?? 'none'} (${reason})`;
	return levels === undefined ? base : `${base}: master ${_fmtDb(levels.masterDb)} dB, deck ${to} ${_fmtDb(levels.toDb)} dB`;
}
