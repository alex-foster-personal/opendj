/**
 * Flanger and phaser live grid anchor (FXMOD): keeps every live flanger or
 * phaser instance phase-locked to its deck.
 *
 * On engage it posts an LFO anchor (`lfoAnchor` from mod-fx-lfo.ts) to the
 * effect's graph node. A 4 Hz poll, running only while an instance is live,
 * re-anchors when the deck's effective tempo moves by 0.05 BPM or more
 * (carrying the beat forward, so the phase never jumps) and re-locks to the
 * grid with the graph's 10 ms wet crossfade when the playhead lands more than
 * 50 ms from where it should be (seek, beat jump, loop wrap, hot cue). If the
 * tempo vanishes mid-effect the last anchor keeps running. With no grid, no
 * downbeat or no tempo in 40..300 BPM, and no time_ms, the instance is inert
 * and `modFxView()` names why.
 *
 * Spec: `(private path)` section 4.
 * Mimicked references: rekordbox AUTO BPM mode (rekordbox 7.2.8 manual p.179)
 * and the house color ECHO tempo law (color-fx.svelte.ts).
 *
 * The graph node is injected as a `ModFxNodePort`, so this module never builds audio.
 */
import { DECK_IDS, type DeckId } from '$lib/player/constants';
import { deckEffectiveBpm, getDeckState } from '$lib/player/state.svelte';
import type { AnlzBeat } from './anlz-types';
import { gridBeatPosition } from './beat-sync-math';
import {
	MOD_FX_MAX_BPM,
	MOD_FX_MIN_BPM,
	lfoAnchor,
	lfoCycleSeconds,
	lfoPhase,
	lfoTimeAnchor,
	type LfoAnchor,
	type ModFxCycleBeats
} from './mod-fx-lfo';

/** 4 Hz, the ECHO law. Polled, not reactive: the effective BPM is derived from the playhead. */
const TEMPO_POLL_MS = 250;
const TEMPO_EPSILON_BPM = 0.05;
/** A playhead off its extrapolated position by more than this re-locks to the grid. */
const JUMP_THRESHOLD_SEC = 0.05;

export const MOD_FX_NO_GRID_REASON =
	'the LFO is timed to the beatgrid and this deck has none: load an analysed track, or set time_ms';
export const MOD_FX_NO_DOWNBEAT_REASON =
	"the LFO locks to downbeats and this deck's beatgrid has none: re-analyse the track, or set time_ms";
export const MOD_FX_NO_TEMPO_REASON =
	'the LFO is timed in beats and this deck has no tempo in 40..300 BPM: load a track with a BPM, or set time_ms';
export const MOD_FX_OFF_GRID_REASON =
	'the playhead is outside the beatgrid, so the LFO cannot lock: move inside the grid, or set time_ms';

export type ModFxEffect = 'flanger' | 'phaser';

/** What the flanger / phaser graph node must offer; injected so this module never builds audio. */
export interface ModFxNodePort {
	effect: ModFxEffect;
	cycleBeats: ModFxCycleBeats;
	/** Free cycle in ms (10..32000); when set the LFO ignores the grid and tempo. */
	timeMs: number | null;
	context: { readonly currentTime: number };
	postAnchor(anchor: LfoAnchor): void;
	/** Ask the graph for its 10 ms wet crossfade (a re-lock after a jump). */
	crossfadeWet(): void;
}

export interface ModFxInstanceView {
	id: number;
	deck: DeckId;
	effect: ModFxEffect;
	/** Cycle in beats, null in time_ms mode. */
	beats: ModFxCycleBeats | null;
	cycle_ms: number | null;
	/** Tempo of the last posted beat anchor. */
	tempo_bpm: number | null;
	lfo_phase: number | null;
	inert_reason: string | null;
}

export interface ModFxHandle {
	readonly id: number;
	detach(): void;
}

interface Instance {
	id: number;
	deck: DeckId;
	node: ModFxNodePort;
	anchor: LfoAnchor | null;
	tempoBpm: number | null;
	inertReason: string | null;
	lastPositionSec: number;
	lastContextTime: number;
	lastRate: number;
}

interface DeckReading {
	beats: readonly AnlzBeat[];
	positionSec: number;
	/** Track seconds per context second, for extrapolating the playhead. */
	rate: number;
}

const _instances = new Map<number, Instance>();
let _nextId = 1;
let _pollTimer: ReturnType<typeof setInterval> | null = null;

//-----------------------------------------------------------------------------
// deck reading

function _deckTempo(deck: DeckId): number | null {
	const bpm = deckEffectiveBpm(deck);
	return bpm !== null && bpm >= MOD_FX_MIN_BPM && bpm <= MOD_FX_MAX_BPM ? bpm : null;
}

function _deckReading(deck: DeckId): DeckReading {
	const st = getDeckState(deck);
	return {
		beats: st.anlz?.beatgrid.beats ?? [],
		positionSec: Math.max(0, st.position_ms / 1000),
		rate: st.playing ? st.pitch : 0
	};
}

/** Why `lfoAnchor` returned null; called only after it did. */
function _inertReason(
	beats: readonly AnlzBeat[],
	positionSec: number,
	tempo: number | null
): string {
	if (beats.length < 2) return MOD_FX_NO_GRID_REASON;
	if (!beats.some((beat) => beat.n === 1)) return MOD_FX_NO_DOWNBEAT_REASON;
	if (tempo === null) return MOD_FX_NO_TEMPO_REASON;
	if (gridBeatPosition(beats, positionSec) === null) return MOD_FX_OFF_GRID_REASON;
	return MOD_FX_NO_TEMPO_REASON;
}

function _remember(inst: Instance, reading: DeckReading, now: number): void {
	inst.lastPositionSec = reading.positionSec;
	inst.lastContextTime = now;
	inst.lastRate = reading.rate;
}

//-----------------------------------------------------------------------------
// anchoring

/** The one grid-lock path: post a fresh anchor from the playhead, or record why it cannot. */
function _lock(inst: Instance, now: number, crossfade: boolean): void {
	const reading = _deckReading(inst.deck);
	_remember(inst, reading, now);
	if (inst.node.timeMs !== null) {
		inst.anchor = lfoTimeAnchor(inst.node.timeMs, now);
		inst.inertReason = null;
		inst.node.postAnchor(inst.anchor);
		return;
	}
	const tempo = _deckTempo(inst.deck);
	const fresh =
		tempo === null
			? null
			: lfoAnchor(reading.beats, reading.positionSec, tempo, now, inst.node.cycleBeats);
	if (fresh === null) {
		// A jump that lands off the grid keeps the old anchor playing (the tail rule).
		if (inst.anchor === null)
			inst.inertReason = _inertReason(reading.beats, reading.positionSec, tempo);
		return;
	}
	inst.anchor = fresh;
	inst.tempoBpm = tempo;
	inst.inertReason = null;
	inst.node.postAnchor(fresh);
	if (crossfade) inst.node.crossfadeWet();
}

/** A tempo move with no jump: carry the old anchor's beat forward, so the phase does not step. */
function _retempo(inst: Instance, now: number, tempo: number): void {
	const old = inst.anchor;
	if (old === null || old.kind !== 'beat') return;
	const beatIndex = old.beatIndex + (now - old.contextTime) * old.beatsPerSec;
	inst.anchor = { ...old, contextTime: now, beatIndex, beatsPerSec: tempo / 60 };
	inst.tempoBpm = tempo;
	inst.node.postAnchor(inst.anchor);
}

// The poll runs at 4 Hz but posts only on a tempo move or a jump: re-posting
// from position_ms every tick would add phase jitter.
function _tick(): void {
	for (const inst of _instances.values()) {
		if (inst.node.timeMs !== null) continue;
		const now = inst.node.context.currentTime;
		if (inst.anchor === null) {
			_lock(inst, now, false);
			continue;
		}
		const reading = _deckReading(inst.deck);
		const expected = inst.lastPositionSec + (now - inst.lastContextTime) * inst.lastRate;
		if (Math.abs(reading.positionSec - expected) > JUMP_THRESHOLD_SEC) {
			_lock(inst, now, true);
			continue;
		}
		const tempo = _deckTempo(inst.deck);
		// The tempo went away mid-effect (a deck unload): keep the last timing so the
		// tail plays at the time it was played, rather than guessing a new one.
		if (
			tempo !== null &&
			(inst.tempoBpm === null || Math.abs(tempo - inst.tempoBpm) >= TEMPO_EPSILON_BPM)
		) {
			_retempo(inst, now, tempo);
		}
		_remember(inst, reading, now);
	}
}

function _syncPoll(): void {
	const want = _instances.size > 0;
	if (want && _pollTimer === null) _pollTimer = setInterval(_tick, TEMPO_POLL_MS);
	if (!want && _pollTimer !== null) {
		clearInterval(_pollTimer);
		_pollTimer = null;
	}
}

//-----------------------------------------------------------------------------
// public

/** Engage: lock a flanger or phaser instance on `deck` to its grid and keep it locked. */
export function attachModFx(deck: DeckId, node: ModFxNodePort): ModFxHandle {
	if (!(DECK_IDS as readonly number[]).includes(deck))
		throw new Error(`mod-fx: deck must be one of ${DECK_IDS.join(', ')}, got ${String(deck)}`);
	if (node === null || typeof node !== 'object')
		throw new Error('mod-fx: node must be a ModFxNodePort');
	if (node.effect !== 'flanger' && node.effect !== 'phaser')
		throw new Error(`mod-fx: node.effect must be flanger or phaser, got ${String(node.effect)}`);
	if (typeof node.postAnchor !== 'function')
		throw new Error('mod-fx: node.postAnchor must be a function');
	if (typeof node.crossfadeWet !== 'function')
		throw new Error('mod-fx: node.crossfadeWet must be a function');
	if (node.context === null || typeof node.context?.currentTime !== 'number')
		throw new Error('mod-fx: node.context.currentTime must be a number');
	const id = _nextId++;
	const inst: Instance = {
		id,
		deck,
		node,
		anchor: null,
		tempoBpm: null,
		inertReason: null,
		lastPositionSec: 0,
		lastContextTime: 0,
		lastRate: 0
	};
	_instances.set(id, inst);
	_lock(inst, node.context.currentTime, false);
	_syncPoll();
	return {
		id,
		detach(): void {
			if (_instances.delete(id)) _syncPoll();
		}
	};
}

/** Per live instance, in attach order. */
export function modFxView(): ModFxInstanceView[] {
	return [..._instances.values()].map((inst) => {
		const timeMode = inst.node.timeMs !== null;
		let cycleMs: number | null = null;
		if (timeMode) {
			cycleMs = inst.node.timeMs;
		} else if (inst.anchor !== null && inst.tempoBpm !== null) {
			const sec = lfoCycleSeconds(inst.node.cycleBeats, inst.tempoBpm);
			cycleMs = sec === null ? null : Math.round(sec * 100000) / 100;
		}
		return {
			id: inst.id,
			deck: inst.deck,
			effect: inst.node.effect,
			beats: timeMode ? null : inst.node.cycleBeats,
			cycle_ms: cycleMs,
			tempo_bpm: timeMode ? null : inst.tempoBpm,
			lfo_phase: inst.anchor === null ? null : lfoPhase(inst.anchor, inst.node.context.currentTime),
			inert_reason: inst.inertReason
		};
	});
}

/** Test seams. */
export function _tickModFxForTests(): void {
	_tick();
}
export function _modFxPollLiveForTests(): boolean {
	return _pollTimer !== null;
}
export function _resetModFxForTests(): void {
	if (_pollTimer !== null) clearInterval(_pollTimer);
	_pollTimer = null;
	_instances.clear();
	_nextId = 1;
}
