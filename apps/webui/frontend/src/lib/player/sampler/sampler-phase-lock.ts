/**
 * Edge sampler: phase lock of a sounding voice to the master (spec 4.3) and the loop seam click
 * check (spec 4.4, section 9 Q2 veto). Issue #10781 (S4). Pure: the voice bank feeds it views of
 * the master on its 10 Hz tick and applies what it returns.
 *
 * The law is the deck's Beat Sync follower lock (`$lib/rb/phase-lock.ts`, NAE-19), mimicked:
 * inside the 2 ms deadband the rate is the base exactly; past it a proportional trim removes the
 * error over 4 master beats and holds until the error is under 1 ms (hysteresis); a new trim is
 * sent only when it moved by more than 0.05 percent of the base. The cap is the spec's 0.5
 * percent (the deck caps its varispeed followers at 0.3). A master jump, jog or loop, a change of
 * master, or an error past the deck's 15 ms re-seek line re-phases the voice at the master's next
 * beat instead of trimming.
 *
 * Phase is counted in master beats: the voice's beats since its origin over `k` (voice beats per
 * master beat, a power of two from the half/double-time fold) against the master's fractional
 * beat index since its origin, wrapped to half a beat.
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ 🎯 a voice against a steady master holds under 1 ms and the trim never passes 0.5 percent
 *     [if] 64 bars play against a steady 128 BPM master [then] every tick reads under 1 ms [else ⛔️]
 *     [if] the master's BPM is 1 percent off its grid [then] the trim stops at 0.5 percent [else ⛔️]
 *   ✔︎ ✅ 🎯 a master tempo move is followed on the next tick (within 100 ms)
 *     [if] the master steps 128 to 132 BPM [then] the voice holds under 2 ms with no re-phase [else ⛔️]
 *   ✔︎ ✅ 🎯 a master jump, jog, loop or change of master re-phases at the next beat
 *     [if] the master seeks 1.37 beats [then] a re-phase lands on its next beat, then under 1 ms [else ⛔️]
 *   ✔︎ ✅ 🎯 a loop seam is checked for a click (a sample step above 0.1 FS within 5 ms)
 *     [if] a loop is whole sine cycles [then] no click [else ⛔️]
 *     [if] a loud wave is cut mid-cycle [then] the seam reads as a click [else ⛔️]
 */

import { nextLaunchCtxSec, type GridBeat } from './sampler-launch';

/** Errors inside this are left alone (mimicked: the deck's PHASE_LOCK_DEADBAND_MS; spec 4.3). */
export const PHASE_LOCK_DEADBAND_MS = 2;
/** A running trim holds until the error is under this (mimicked: PHASE_LOCK_RELEASE_MS; spec 4.3). */
export const PHASE_LOCK_RELEASE_MS = 1;
/** Proportional gain: remove the error over this many master beats (mimicked: deck lock). */
export const PHASE_LOCK_CORRECTION_BEATS = 4;
/** Largest trim, as a fraction of the base rate (spec 4.3: at most 0.5 percent). */
export const PHASE_LOCK_MAX_TRIM = 0.005;
/** An error or a master discontinuity past this re-phases (mimicked: PHASE_LOCK_RESEEK_MS). */
export const PHASE_LOCK_REPHASE_MS = 15;
/** A trim is re-sent only when it moved by more than this fraction of the base (deck lock). */
export const PHASE_LOCK_RESEND_RATIO = 0.0005;
/** Click veto: a sample step above this (full scale) near the seam (spec section 9, Q2). */
export const SEAM_CLICK_FS = 0.1;
/** ...within this many seconds either side of the seam (spec section 9, Q2). */
export const SEAM_WINDOW_SEC = 0.005;

/** The playing master at one context time. */
export interface MasterView {
	/** deck number, so a change of master re-phases */
	deck: number;
	beats: readonly GridBeat[];
	positionSec: number;
	tempoRatio: number;
}

interface MasterMark {
	deck: number;
	ctx: number;
	beat: number;
	beatsPerSec: number;
}

export interface PhaseLockState {
	/** clip seconds per voice beat */
	voiceBeatSec: number;
	/** voice beats per master beat (1, 2, 0.5, ...) */
	k: number;
	/** context time `voiceBeats` was counted to */
	ctx: number;
	/** voice beats since launch (unwrapped) */
	voiceBeats: number;
	originVoice: number;
	originMaster: number;
	trimming: boolean;
	/** rate last sent to the voice */
	sentRate: number;
	/** base rate when `sentRate` was sent, so a tempo move is always sent */
	sentBase: number;
	master: MasterMark | null;
	pending: { at: number; voiceBeat: number; masterBeat: number } | null;
}

export type PhaseLockStep = {
	state: PhaseLockState;
	/** the rate to send now, or null to leave the voice alone */
	rate: number | null;
	/** the measured error in ms (null when it was not measured this tick) */
	errorMs: number | null;
	/** re-phase: at context time `at` the voice moves to voice beat `voiceBeat` */
	rephase: { at: number; voiceBeat: number } | null;
};

//----- master grid

function _interval(beats: readonly GridBeat[], i: number): number {
	const d = beats[i + 1].t - beats[i].t;
	if (!(d > 0)) throw new Error(`master beat grid is not increasing at beat ${i}`);
	return d;
}

/** Fractional beat index of a track position on a grid (extrapolated past either end). */
export function masterBeatAt(beats: readonly GridBeat[], positionSec: number): number {
	if (beats.length < 2) throw new Error('the master deck has no beat grid');
	let lo = 0;
	let hi = beats.length - 2;
	while (lo < hi) {
		const mid = (lo + hi + 1) >> 1;
		if (beats[mid].t <= positionSec) lo = mid;
		else hi = mid - 1;
	}
	const i = lo;
	return i + (positionSec - beats[i].t) / _interval(beats, i);
}

/** Master beats per output second at a track position. */
export function masterBeatsPerSec(m: MasterView): number {
	const i = Math.max(
		0,
		Math.min(Math.floor(masterBeatAt(m.beats, m.positionSec)), m.beats.length - 2)
	);
	return m.tempoRatio / _interval(m.beats, i);
}

const _mark = (m: MasterView, ctx: number): MasterMark => ({
	deck: m.deck,
	ctx,
	beat: masterBeatAt(m.beats, m.positionSec),
	beatsPerSec: masterBeatsPerSec(m)
});

/** Nearest power of two, so a folded half/double-time voice counts whole master beats. */
const _pow2 = (x: number): number => 2 ** Math.round(Math.log2(x));
const _wrapHalf = (x: number): number => x - Math.round(x);

//----- lock

/** The lock for a voice launched at `at` (a master beat) at `rate`, with the master seen at `at`. */
export function startPhaseLock(
	at: number,
	rate: number,
	voiceBeatSec: number,
	master: MasterView
): PhaseLockState {
	if (!(voiceBeatSec > 0) || !(rate > 0))
		throw new Error(`phase lock needs a positive beat and rate, got ${voiceBeatSec} / ${rate}`);
	const mark = _mark(master, at);
	return {
		voiceBeatSec,
		k: _pow2(rate / voiceBeatSec / mark.beatsPerSec),
		ctx: at,
		voiceBeats: 0,
		originVoice: 0,
		originMaster: Math.round(mark.beat),
		trimming: false,
		sentRate: rate,
		sentBase: rate,
		master: mark,
		pending: null
	};
}

/** The proportional trim for an error in master beats, capped at PHASE_LOCK_MAX_TRIM. */
export function trimFor(errorBeats: number): number {
	const t = -errorBeats / PHASE_LOCK_CORRECTION_BEATS;
	return Math.max(-PHASE_LOCK_MAX_TRIM, Math.min(PHASE_LOCK_MAX_TRIM, t));
}

/** True when the master moved other than it was playing: a seek, jog, loop or a new master deck. */
export function masterJumped(last: MasterMark | null, now: MasterMark): boolean {
	if (last === null || last.deck !== now.deck) return true;
	const expected = last.beat + (now.ctx - last.ctx) * last.beatsPerSec;
	return (Math.abs(now.beat - expected) / now.beatsPerSec) * 1000 > PHASE_LOCK_REPHASE_MS;
}

function _rephase(s: PhaseLockState, m: MasterView, now: number, base: number): PhaseLockStep {
	const at = nextLaunchCtxSec(m.beats, m.positionSec, now, m.tempoRatio, 'beat', 0);
	const predicted = s.voiceBeats + ((at - now) * base) / s.voiceBeatSec;
	const voiceBeat = Math.round(predicted / s.k) * s.k;
	const masterBeat = Math.round(masterBeatAt(m.beats, m.positionSec + (at - now) * m.tempoRatio));
	const held = _hold(s, base);
	const state = { ...held.state, pending: { at, voiceBeat, masterBeat } };
	return { ...held, state, rephase: { at, voiceBeat } };
}

/** No trim: the voice plays the base rate (sent only when it differs from what it plays). */
function _hold(s: PhaseLockState, base: number): PhaseLockStep {
	const state = { ...s, trimming: false, sentRate: base, sentBase: base };
	return { state, rate: base === s.sentRate ? null : base, errorMs: null, rephase: null };
}

/**
 * One 10 Hz tick. `base` is the tempo-matched rate (spec 4.3), `master` the playing master now
 * (null: none plays, the voice holds its rate). Returns the next state and what to send.
 */
export function phaseLockTick(
	s0: PhaseLockState,
	now: number,
	base: number,
	master: MasterView | null
): PhaseLockStep {
	if (now <= s0.ctx) return _hold(s0, base); // armed, not sounding yet
	let s: PhaseLockState = {
		...s0,
		ctx: now,
		voiceBeats: s0.voiceBeats + ((now - s0.ctx) * s0.sentRate) / s0.voiceBeatSec
	};
	if (s.pending !== null && now >= s.pending.at) {
		const p = s.pending;
		s = {
			...s,
			voiceBeats: p.voiceBeat + ((now - p.at) * s0.sentRate) / s.voiceBeatSec,
			originVoice: p.voiceBeat,
			originMaster: p.masterBeat,
			pending: null
		};
	}
	if (master === null) return _hold({ ...s, master: null }, base);
	const mark = _mark(master, now);
	const jumped = masterJumped(s.master, mark);
	s = { ...s, master: mark };
	if (s.pending !== null) return _hold(s, base);
	if (jumped) return _rephase(s, master, now, base);
	const errorBeats = _wrapHalf((s.voiceBeats - s.originVoice) / s.k - (mark.beat - s.originMaster));
	const errorMs = (errorBeats / mark.beatsPerSec) * 1000;
	if (Math.abs(errorMs) > PHASE_LOCK_REPHASE_MS) return _rephase(s, master, now, base);
	const trimming = s.trimming
		? Math.abs(errorMs) >= PHASE_LOCK_RELEASE_MS
		: Math.abs(errorMs) > PHASE_LOCK_DEADBAND_MS;
	const rate = trimming ? base * (1 + trimFor(errorBeats)) : base;
	const send =
		rate === base
			? rate !== s.sentRate
			: base !== s.sentBase || Math.abs(rate - s.sentRate) > PHASE_LOCK_RESEND_RATIO * base;
	return {
		state: {
			...s,
			trimming,
			sentRate: send ? rate : s.sentRate,
			sentBase: send ? base : s.sentBase
		},
		rate: send ? rate : null,
		errorMs,
		rephase: null
	};
}

/** Clip seconds a looping voice plays at voice beat `voiceBeat`, or null for a play-once voice. */
export function rephaseClipSec(
	plan: { guardSec: number; loop: boolean; loopStartSec: number; loopEndSec: number },
	voiceBeat: number,
	voiceBeatSec: number
): number | null {
	if (!plan.loop) return null;
	const span = plan.loopEndSec - plan.loopStartSec;
	const into = (((voiceBeat * voiceBeatSec) % span) + span) % span;
	return plan.guardSec + plan.loopStartSec + into;
}

//----- seam

/** Largest sample step across a loop seam played as the source loops it (end tail, then start head). */
export function seamStepFs(
	channels: readonly Float32Array[],
	sampleRate: number,
	loopStartSec: number,
	loopEndSec: number
): number {
	const start = Math.round(loopStartSec * sampleRate);
	const end = Math.round(loopEndSec * sampleRate);
	const w = Math.round(SEAM_WINDOW_SEC * sampleRate);
	if (!(end - start > 2 * w))
		throw new Error(`loop of ${end - start} frames is shorter than the seam window`);
	let worst = 0;
	for (const ch of channels) {
		if (end > ch.length) throw new Error(`loop end ${end} is past the clip (${ch.length} frames)`);
		const stitched = [...ch.subarray(end - w, end), ...ch.subarray(start, start + w)];
		for (let n = 1; n < stitched.length; n += 1)
			worst = Math.max(worst, Math.abs(stitched[n] - stitched[n - 1]));
	}
	return worst;
}

/** True when the loop seam clicks (spec section 9, Q2 hard veto). */
export function seamClicks(
	channels: readonly Float32Array[],
	sampleRate: number,
	loopStartSec: number,
	loopEndSec: number
): boolean {
	return seamStepFs(channels, sampleRate, loopStartSec, loopEndSec) > SEAM_CLICK_FS;
}
