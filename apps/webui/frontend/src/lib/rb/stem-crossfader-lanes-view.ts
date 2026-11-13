/** STEMPERF-14650: pure view for the stem crossfader lanes above the main
 * crossfader (#10328). Decides which lanes exist (stems BOTH A/B decks can
 * drive), why the rest are inert, whether the lanes are open (hover, or any
 * lane unlinked) and which `stem_crossfader` intent a gesture dispatches.
 * Nothing here touches the DOM or the engine. */
import { STEM_CONTROL_IDS, type StemControl } from './stem-types';

export type LaneInertReason = 'mixed into INST' | 'not in both stem-sets';
export const NO_DECK_REASON = 'load a stem-set on both A and B decks';

export interface StemLane {
	stem: StemControl;
	linked: boolean;
	glyph: 'link' | 'unlink';
	/** 0..1 lane position, meaningful when unlinked. */
	value: number;
}

export interface StemLanesView {
	open: boolean;
	lanes: StemLane[];
	inert: { stem: StemControl; reason: LaneInertReason }[];
	/** Set when no lane can exist at all; one reason, not one per stem. */
	reason: string | null;
}

export interface StemLanesInput {
	/** `available_controls` of the deck on bus A / bus B, null when none is loaded. */
	a: readonly StemControl[] | null;
	b: readonly StemControl[] | null;
	/** Mirror `mixer.stem_crossfader`: unlinked lanes and their positions. */
	lanes?: Partial<Record<StemControl, { linked: boolean; value: number }>>;
	hover: boolean;
}

/** Instrumental-only layouts fold these into INST rather than lacking them. */
const FOLDED_INTO_INST: readonly StemControl[] = ['drums', 'bass', 'other'];

/** Build the lane view from the two decks' controls, link state and hover. */
export function stemCrossfaderLanesView(input: StemLanesInput): StemLanesView {
	const { a, b, hover } = input;
	if (!a || !b || a.length === 0 || b.length === 0) {
		return { open: false, lanes: [], inert: [], reason: NO_DECK_REASON };
	}
	const lanes: StemLane[] = [];
	const inert: StemLanesView['inert'] = [];
	for (const stem of STEM_CONTROL_IDS) {
		if (a.includes(stem) && b.includes(stem)) {
			const state = input.lanes?.[stem];
			const linked = state?.linked ?? true;
			lanes.push({
				stem,
				linked,
				glyph: linked ? 'link' : 'unlink',
				value: clamp01(state?.value ?? 0.5)
			});
		} else {
			const instOnly = (l: readonly StemControl[]) =>
				!l.includes(stem) && l.includes('instrumental');
			const folded = FOLDED_INTO_INST.includes(stem) && (instOnly(a) || instOnly(b));
			inert.push({ stem, reason: folded ? 'mixed into INST' : 'not in both stem-sets' });
		}
	}
	const open = hover || lanes.some((lane) => !lane.linked);
	return { open, lanes, inert, reason: null };
}

export type StemCrossfaderIntent =
	| { type: 'stem_crossfader'; stem: StemControl; value: number; linked: false }
	| { type: 'stem_crossfader'; stem: StemControl; linked: true };

/** Drag or wheel on a lane: move it and unlink it. */
export function laneDragIntent(stem: StemControl, value: number): StemCrossfaderIntent {
	return { type: 'stem_crossfader', stem, value: clamp01(value), linked: false };
}

/** Double-click on a lane: re-link it to the main crossfader. */
export function laneRelinkIntent(stem: StemControl): StemCrossfaderIntent {
	return { type: 'stem_crossfader', stem, linked: true };
}

function clamp01(v: number): number {
	return Number.isFinite(v) ? Math.min(1, Math.max(0, v)) : 0.5;
}
