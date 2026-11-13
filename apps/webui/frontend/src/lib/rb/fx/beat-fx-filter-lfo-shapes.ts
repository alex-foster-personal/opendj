/**
 * LFO FILTER shapes and directions (#10014): the cutoff trajectory of the beat FX
 * FILTER kernel as a pure function of LFO phase, on the octave axis in units of the kernel's
 * half-span (-1 = the LP floor, +1 = the LP ceiling).
 *
 * Mimicked: Traktor Filter LFO (Traktor Pro manual, Effect Reference): SHAPE runs from a
 * triangle to a sawtooth with a sharp decay, UP inverts the sweep. The DJM-A9 (manual p.54)
 * publishes no shape. Defaults sine and down are the #9792 trajectory, unchanged: phase 0 is
 * the trough (the mod-fx-lfo rule, Traktor LFO RESET to the lowest point).
 *
 * - sine: -cos(2 pi phase).
 * - triangle: linear from the trough at phase 0 to the crest at 0.5 and back.
 * - saw_decay: a sharp fall at phase 0 (a raised cosine over `resetPhase` cycles, the kernel's
 *   10 ms ramp), then a linear rise from the trough back to the crest.
 * - up mirrors the trajectory on the octave axis about the sweep centre.
 */

export const FILTER_LFO_SHAPES = ['sine', 'triangle', 'saw_decay'] as const;
export type FilterLfoShape = (typeof FILTER_LFO_SHAPES)[number];

export const FILTER_LFO_DIRECTIONS = ['down', 'up'] as const;
export type FilterLfoDirection = (typeof FILTER_LFO_DIRECTIONS)[number];

/** Spec default (06-filter.md section 4: sine on the octave axis), the #9792 trajectory. */
export const FILTER_LFO_DEFAULT_SHAPE: FilterLfoShape = 'sine';
export const FILTER_LFO_DEFAULT_DIRECTION: FilterLfoDirection = 'down';

/** Throws naming the value unless `shape` is one of FILTER_LFO_SHAPES. */
export function assertFilterLfoShape(shape: unknown): asserts shape is FilterLfoShape {
	if (!(FILTER_LFO_SHAPES as readonly unknown[]).includes(shape)) {
		throw new Error(
			`beat-fx-filter: lfoShape must be one of ${FILTER_LFO_SHAPES.join(', ')}, got ${String(shape)}`
		);
	}
}

/** Throws naming the value unless `direction` is one of FILTER_LFO_DIRECTIONS. */
export function assertFilterLfoDirection(
	direction: unknown
): asserts direction is FilterLfoDirection {
	if (!(FILTER_LFO_DIRECTIONS as readonly unknown[]).includes(direction)) {
		throw new Error(
			`beat-fx-filter: lfoDirection must be one of ${FILTER_LFO_DIRECTIONS.join(', ')}, got ${String(direction)}`
		);
	}
}

/** LFO value in -1..1 (half-spans on the octave axis) at `phase`; `resetPhase` is the saw_decay fall width in cycles. */
export function filterLfoUnit(
	shape: FilterLfoShape,
	direction: FilterLfoDirection,
	phase: number,
	resetPhase = 0
): number {
	if (!Number.isFinite(phase))
		throw new Error(`beat-fx-filter: phase must be finite, got ${String(phase)}`);
	if (!(resetPhase >= 0 && resetPhase < 1)) {
		throw new Error(`beat-fx-filter: resetPhase must be within 0..1, got ${String(resetPhase)}`);
	}
	assertFilterLfoDirection(direction);
	const p = phase - Math.floor(phase);
	const sign = direction === 'up' ? -1 : 1;
	switch (shape) {
		case 'sine':
			return sign * -Math.cos(2 * Math.PI * phase);
		case 'triangle':
			return sign * (1 - 4 * Math.abs(p - 0.5));
		case 'saw_decay':
			if (p < resetPhase) return sign * Math.cos((Math.PI * p) / resetPhase);
			return sign * (-1 + (2 * (p - resetPhase)) / (1 - resetPhase));
		default: {
			const _exhaustive: never = shape;
			throw new Error(
				`beat-fx-filter: lfoShape must be one of ${FILTER_LFO_SHAPES.join(', ')}, got ${String(_exhaustive)}`
			);
		}
	}
}
