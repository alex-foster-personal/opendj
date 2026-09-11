/**
 * SLIP hidden-transport anchor algebra.
 *
 * Extracted whole from `$lib/rb/audio-engine.svelte.ts` so the engine stays
 * under the file-size ratchet. Every function here is pure: it takes an anchor
 * (plus acknowledged rate boundaries) and returns the hidden playhead the
 * engine would have computed inline. Nothing in this module touches the
 * AudioContext, the deck runtime, or a rune store, so it is directly unit
 * testable without booting the engine.
 *
 * Reached by the engine through the `$lib/player/transport/presentation`
 * barrel, which re-exports this surface. This module deliberately imports
 * NOTHING, so the presentation -> slip-anchor edge cannot become a cycle.
 */

/** A confirmed loop-entry schedule is the sole anchor for SLIP's hidden
 * playhead. It deliberately uses control time, not the audible UI clock. */
export interface SlipAnchor {
	startContextTime: number;
	startPositionSec: number;
	tempoRatio: number;
	durationSec: number;
}

/** Acknowledged future rate change for SLIP's hidden, non-looping timeline. */
export interface SlipTempoBoundary {
	startContextTime: number;
	tempoRatio: number;
}

export function shouldActivateSlip(playing: boolean, slipEnabled: boolean): boolean {
	if (typeof playing !== 'boolean' || typeof slipEnabled !== 'boolean') {
		throw new TypeError('SLIP activation inputs must be boolean');
	}
	return playing && slipEnabled;
}

export function createSlipAnchor(input: SlipAnchor): SlipAnchor {
	for (const [name, value] of Object.entries(input)) {
		if (!Number.isFinite(value)) throw new RangeError(`${name} must be finite, got ${value}`);
	}
	if (input.startContextTime < 0 || input.startPositionSec < 0) {
		throw new RangeError('SLIP anchor time and position must be non-negative');
	}
	if (input.tempoRatio <= 0) throw new RangeError('tempoRatio must be positive');
	if (input.durationSec <= 0 || input.startPositionSec > input.durationSec) {
		throw new RangeError('SLIP anchor position must be within a positive decoded duration');
	}
	return { ...input };
}

/** Hidden SLIP time always advances linearly and clamps at decoded EOF. It
 * never uses loop normalization or replaces the output-presented cursor. */
export function slipHiddenPositionSec(anchor: SlipAnchor, contextTime: number): number {
	const validAnchor = createSlipAnchor(anchor);
	if (!Number.isFinite(contextTime)) {
		throw new RangeError(`contextTime must be finite, got ${contextTime}`);
	}
	const elapsed = Math.max(0, contextTime - validAnchor.startContextTime);
	return Math.min(
		validAnchor.durationSec,
		validAnchor.startPositionSec + elapsed * validAnchor.tempoRatio
	);
}

/** Re-anchor hidden SLIP transport at an acknowledged rate boundary. */
export function rebaseSlipAnchor(
	anchor: SlipAnchor,
	effectiveWhen: number,
	tempoRatio: number
): SlipAnchor {
	if (!Number.isFinite(effectiveWhen) || effectiveWhen < 0) {
		throw new RangeError(`SLIP rebase time must be finite and non-negative, got ${effectiveWhen}`);
	}
	return createSlipAnchor({
		startContextTime: effectiveWhen,
		startPositionSec: slipHiddenPositionSec(anchor, effectiveWhen),
		tempoRatio,
		durationSec: anchor.durationSec
	});
}

/** Integrate hidden SLIP time through its accepted presentation-rate boundaries. */
export function slipHiddenPositionWithTempoBoundaries(
	anchor: SlipAnchor,
	boundaries: readonly SlipTempoBoundary[],
	contextTime: number
): number {
	if (!Number.isFinite(contextTime) || contextTime < 0) {
		throw new RangeError(`SLIP context time must be finite and non-negative, got ${contextTime}`);
	}
	let segment = createSlipAnchor(anchor);
	for (const boundary of boundaries) {
		if (
			!Number.isFinite(boundary.startContextTime) ||
			boundary.startContextTime < segment.startContextTime
		) {
			throw new RangeError('SLIP tempo boundaries must be ordered after the anchor');
		}
		if (!Number.isFinite(boundary.tempoRatio) || boundary.tempoRatio <= 0) {
			throw new RangeError('SLIP tempo boundary ratio must be finite and positive');
		}
		if (boundary.startContextTime > contextTime) break;
		segment = rebaseSlipAnchor(segment, boundary.startContextTime, boundary.tempoRatio);
	}
	return slipHiddenPositionSec(segment, contextTime);
}
