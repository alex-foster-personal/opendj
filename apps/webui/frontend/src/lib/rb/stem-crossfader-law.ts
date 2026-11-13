/** STEMPERF stem crossfader lanes, pure law (split of #10327, part of #10315).
 * Each stem-set part gets its own crossfader lane. A linked lane follows the
 * main crossfader; an unlinked lane reads its own position. The gain law is the
 * same equal-power law the main crossfader applies per bus assignment. No
 * engine, IPC or CLI here: callers multiply the lane gain into the part gain. */
import type { CrossfaderAssign } from './mixer-types';
import { STEM_CONTROL_IDS, type StemControl } from './stem-types';

export interface StemCrossfaderLane {
	/** 0..1, 0 = full A, 1 = full B. Ignored while linked. */
	position: number;
	linked: boolean;
}

export type StemCrossfaderState = Readonly<Record<StemControl, Readonly<StemCrossfaderLane>>>;

export interface StemLaneUpdate {
	value?: number;
	linked?: boolean;
}

function clamp01(x: number): number {
	if (!Number.isFinite(x)) return 0;
	return Math.min(1, Math.max(0, x));
}

/** Equal-power crossfade gain for one bus assignment at position x (0..1). */
export function crossfaderLawGain(assign: CrossfaderAssign, x: number): number {
	if (assign === 'THRU') return 1;
	const p = clamp01(x);
	if (assign === 'A') return Math.cos((p * Math.PI) / 2);
	return Math.cos(((1 - p) * Math.PI) / 2);
}

/** Every lane linked, sitting at the main crossfader position. */
export function defaultStemCrossfaderState(mainPosition = 0.5): StemCrossfaderState {
	const p = clamp01(mainPosition);
	const out = {} as Record<StemControl, StemCrossfaderLane>;
	for (const id of STEM_CONTROL_IDS) out[id] = { position: p, linked: true };
	return out;
}

/** Returns a new state with one lane changed; re-linking snaps it to the main position. */
export function setStemLane(
	state: StemCrossfaderState,
	stem: StemControl,
	update: StemLaneUpdate,
	mainPosition: number
): StemCrossfaderState {
	const prev = state[stem];
	const linked = update.linked ?? (update.value !== undefined ? false : prev.linked);
	let position = update.value !== undefined ? clamp01(update.value) : prev.position;
	if (linked) position = clamp01(mainPosition);
	return { ...state, [stem]: { position, linked } };
}

/** Position a lane is heard at: the main position when linked, its own otherwise. */
export function stemLanePosition(
	state: StemCrossfaderState,
	mainPosition: number,
	stem: StemControl
): number {
	const lane = state[stem];
	return lane.linked ? clamp01(mainPosition) : clamp01(lane.position);
}

/** Per-part multiplier for one deck assignment. */
export function stemLaneGain(
	state: StemCrossfaderState,
	mainPosition: number,
	assign: CrossfaderAssign,
	stem: StemControl
): number {
	return crossfaderLawGain(assign, stemLanePosition(state, mainPosition, stem));
}

/** Per-part multipliers for the A side and the B side of one lane. */
export function stemLaneGains(
	state: StemCrossfaderState,
	mainPosition: number,
	stem: StemControl
): { a: number; b: number } {
	const x = stemLanePosition(state, mainPosition, stem);
	return { a: crossfaderLawGain('A', x), b: crossfaderLawGain('B', x) };
}
