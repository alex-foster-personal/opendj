/**
 * Stem separation contract: which controls a bundle can drive, and the
 * serializable graph read model behind them.
 *
 * Split out of the former lib/rb/types.ts god module.
 */

/** Instrumental remains the real Demucs bass + other parent group. Its
 * children can also be controlled independently, never by subtraction. */
export type StemControl = 'vocal' | 'instrumental' | 'drums' | 'bass' | 'other';
export const STEM_CONTROL_IDS: readonly StemControl[] = ['vocal', 'instrumental', 'drums', 'bass', 'other'];

export interface StemControlState {
	muted: boolean;
	solo: boolean;
	/** 0..1 knob. 0.5 = unity. Independent of mute/solo. */
	gain: number;
}

export interface StemAlignment {
	sample_rate_hz: number;
	frame_count: number;
	channel_count: number;
	duration_ms: number;
}

/** Which separation produced a bundle, and therefore which controls exist.
 * `demucs4` splits vocals/drums/bass/other; `roformer2` splits vocals from a
 * single instrumental that ALREADY CONTAINS the drums, so DRUMS is not a
 * control it can offer -- not a control that happens to be silent. */
export type StemLayout = 'demucs4' | 'roformer2';

/** Serializable stem graph capability and control read model. `ready` means
 * every part of the bundle's OWN layout decoded and aligned -- four parts for
 * demucs4, two for roformer2.
 *
 * `loading` is the SECONDARY-load state: the deck is playable on its mix
 * buffer and the stem bundle is still being probed/fetched/decoded off the
 * critical path. It is deliberately NOT merged into `unavailable`, because a
 * reader has to be able to tell "no bundle exists" (settled, no spinner, no
 * retry) from "not here yet" (in flight, spinner, controls will arrive). */
export interface StemDeckState {
	status: 'unavailable' | 'loading' | 'ready' | 'error';
	source: 'demucs' | 'roformer' | null;
	model: string | null;
	layout: StemLayout | null;
	/** Controls this bundle can actually drive. A control absent from here
	 * MUST render inert: acting on it would be a no-op the user cannot see. */
	available_controls: StemControl[];
	alignment: StemAlignment | null;
	controls: Record<StemControl, StemControlState>;
	error: string | null;
}
