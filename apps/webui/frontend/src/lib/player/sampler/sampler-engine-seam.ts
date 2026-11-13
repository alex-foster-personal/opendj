/**
 * Edge sampler: the audio engine seam the store plays through ((private path) 8; #10784).
 *
 * The store never imports the engine. The real one (sampler-engine.svelte.ts) registers
 * itself through the store's `registerSamplerEngine` when it lands. Until then NO_ENGINE
 * stands in and says so: play and preview reject with SamplerNoEngineError, no slot is
 * ever playing, and stop, stop preview and warm have nothing to act on.
 */

import type { SamplerSlot } from './sampler-types';

/** What a slot's voice is doing: idle, waiting for the quantise point, sounding, or on the cue bus. */
export type SlotPhase = 'free' | 'armed' | 'playing' | 'previewing';

/** The engine calls the store makes (v1 toggleSlot, stopSlot, previewSlot, stopPreview, warmSlots, slotPhase). */
export interface SamplerEngine {
	/** Start the slot on the master at the next quantise point, or stop it when it sounds. */
	toggle(slot: SamplerSlot): Promise<void>;
	stop(slotId: string): void;
	/** Play the slot on the cue bus only. */
	preview(slot: SamplerSlot): Promise<void>;
	stopPreview(): void;
	/** Decode these slots' clips ahead of a press. */
	warm(slots: SamplerSlot[]): void;
	phase(slotId: string): SlotPhase;
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
	warm: () => undefined,
	phase: () => 'free'
};
