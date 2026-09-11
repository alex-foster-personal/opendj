/**
 * Pure axis mapping for the Load-to-CHn click-drag intro blend (issue #286).
 *
 * No DOM, no Svelte, no IPC. Knob range is 0..1 with flat at 0.5.
 */

export const LOAD_BLEND_THRESHOLD_PX = 8;
export const SCRUB_MS_PER_PX = 32;
export const FADER_PX = 120;
export const T_OVERSHOOT = 1.4;

/** Incoming mixer at morph t = 0. High is not written. */
export const LOAD_BLEND_INCOMING_START = {
	fader: 0,
	low: 0.3,
	mid: 0.4,
	filter: 0.65
} as const;

/** Master complementary fill at morph t = 0. Fader/filter/high are not written. */
export const LOAD_BLEND_MASTER_START = {
	low: 0.65,
	mid: 0.6
} as const;

export const LOAD_BLEND_FLAT = 0.5;

/** Channel fields this gesture may restore. High is snapshotted even though we do not write it. */
export type MixerChannelSnapshot = {
	eq_low: number;
	eq_mid: number;
	eq_high: number;
	filter: number;
	fader: number;
	cue_enabled: boolean;
};

export function snapshotMixerChannel(ch: MixerChannelSnapshot): MixerChannelSnapshot {
	return {
		eq_low: ch.eq_low,
		eq_mid: ch.eq_mid,
		eq_high: ch.eq_high,
		filter: ch.filter,
		fader: ch.fader,
		cue_enabled: ch.cue_enabled
	};
}

export function loadBlendProgress(input: { dyPx: number }): { fader: number; t: number } {
	const rawFader = input.dyPx / FADER_PX;
	return {
		fader: clamp(rawFader, 0, 1),
		t: clamp(rawFader / T_OVERSHOOT, 0, 1)
	};
}

export function loadBlendScrubMs(input: {
	dxPx: number;
	originMs: number;
	durationMs: number;
}): number {
	const raw = input.originMs + input.dxPx * SCRUB_MS_PER_PX;
	const hi = input.durationMs > 0 ? input.durationMs : 0;
	return clamp(raw, 0, hi);
}

export function loadBlendEqAt(t: number): {
	incoming: { low: number; mid: number; filter: number };
	master: { low: number; mid: number };
} {
	const u = clamp(t, 0, 1);
	return {
		incoming: {
			low: lerp(LOAD_BLEND_INCOMING_START.low, LOAD_BLEND_FLAT, u),
			mid: lerp(LOAD_BLEND_INCOMING_START.mid, LOAD_BLEND_FLAT, u),
			filter: lerp(LOAD_BLEND_INCOMING_START.filter, LOAD_BLEND_FLAT, u)
		},
		master: {
			low: lerp(LOAD_BLEND_MASTER_START.low, LOAD_BLEND_FLAT, u),
			mid: lerp(LOAD_BLEND_MASTER_START.mid, LOAD_BLEND_FLAT, u)
		}
	};
}

function clamp(n: number, lo: number, hi: number): number {
	return Math.min(hi, Math.max(lo, n));
}

function lerp(start: number, end: number, t: number): number {
	return start + (end - start) * t;
}
