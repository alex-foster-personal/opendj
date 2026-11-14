/**
 * Edge sampler: the audio engine seam the store plays through ((private path) 8; #10784).
 *
 * The stores never import the engine. The real one (sampler-engine.svelte.ts) registers
 * through the edge store's `registerSamplerEngine`, which shares it here so the classic
 * store plays through the same one ((private path) 2). Until then NO_ENGINE
 * stands in and says so: play and preview reject with SamplerNoEngineError, no slot is
 * ever playing, and stop, stop preview and reside have nothing to act on.
 */

import type { ResidencyView } from './sampler-residency';
import type { SamplerSlot } from './sampler-types';

/** What a slot's voice is doing: idle, waiting for the quantise point, sounding, or on the cue bus. */
export type SlotPhase = 'free' | 'armed' | 'playing' | 'previewing';

/** The engine calls the store makes (v1 toggleSlot, stopSlot, previewSlot, stopPreview, warmSlots, slotPhase; warm became reside). */
export interface SamplerEngine {
	/** Start the slot on the master at the next quantise point, or stop it when it sounds. */
	toggle(slot: SamplerSlot): Promise<void>;
	stop(slotId: string): void;
	/** Play the slot on the cue bus only. */
	preview(slot: SamplerSlot): Promise<void>;
	stopPreview(): void;
	/**
	 * The residency view changed (#10782): promote its slots ahead of a press, holding `padPins`
	 * (the store's pinned tabs) with the sounding, armed and previewing slots.
	 */
	reside(view: ResidencyView<SamplerSlot>, padPins: readonly string[]): void;
	phase(slotId: string): SlotPhase;
	/** 0..1 through the slot's current pass on a voice or the preview; null when it is not sounding. */
	playhead(slotId: string): number | null;
}

/** A play or preview asked of the sampler before any audio engine registered. */
export class SamplerNoEngineError extends Error {
	constructor(what: string) {
		super(`${what}: the sampler has no audio engine yet`);
		this.name = 'SamplerNoEngineError';
	}
}

/** The engine before one registers: nothing plays, so play and preview fail loudly. */
export const NO_ENGINE: SamplerEngine = {
	toggle: (slot) => Promise.reject(new SamplerNoEngineError(`play ${slot.slot_id}`)),
	stop: () => undefined,
	preview: (slot) => Promise.reject(new SamplerNoEngineError(`preview ${slot.slot_id}`)),
	stopPreview: () => undefined,
	reside: () => undefined,
	phase: () => 'free',
	playhead: () => null
};

let _shared: SamplerEngine = NO_ENGINE;

/** Make `engine` the one both sampler surfaces play through; NO_ENGINE unplugs it. */
export function shareSamplerEngine(engine: SamplerEngine): void {
	_shared = engine;
}

/** The engine both sampler surfaces play through now; NO_ENGINE until one registers. */
export const sharedSamplerEngine = (): SamplerEngine => _shared;
