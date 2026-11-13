/**
 * Beat FX unit graph (issue #9782, spec `fx-fanout-20261108/01-beat-fx.md` section 4.2):
 * per-channel post-fader dry and send nodes, one kernel slot, the wet return on masterGain.
 *
 * ```
 * colorBus -> cue (pre-fader)
 * colorBus -> fader -+-> dry (1.0) -> xf -> masterGain
 *                    +-> send (0.0) -> unitIn -> kernel -> masterGain
 * ```
 *
 * Post-fader send: a fader cut stops new input while the tail rings, and the cue taps the
 * bus before the fader, so the effect is never in the channel cue (DJM-A9 manual p. 56
 * footnotes 1 and 2, mimicked). The wet joins masterGain and nothing reads masterGain back
 * into a send, so no target can feed back. The master target opens every channel's send
 * (no preFxSum node: audio-engine.svelte.ts is at its size cap). Built only when
 * `fx.beat_rack` is on; dry gains are exactly 1 and sends exactly 0 until the unit opens.
 */
import type { DeckId } from './deck-id';
import type { BeatFxTarget } from './beat-fx';
import { scheduleGainStep } from './beat-fx-quantize';
import { beatFxTimingMs, type BeatFxUnitState } from './beat-fx-unit';
import { beatFxEffectDef, type BeatFxKernel } from './fx/beat-fx-effects';

export type { BeatFxKernel } from './fx/beat-fx-effects';

export interface BeatFxInsert {
	readonly ctx: BaseAudioContext;
	readonly masterGain: AudioNode;
	/** Sum of the channel sends; the slot's kernel reads it. */
	readonly unitIn: GainNode;
	readonly sends: Map<DeckId, GainNode>;
	readonly drys: Map<DeckId, GainNode>;
	slot: { effect: string; kernel: BeatFxKernel } | null;
}

let _live: BeatFxInsert | null = null;

/** The insert of the graph built last, or null when `fx.beat_rack` was off at build. */
export function liveBeatFxInsert(): BeatFxInsert | null {
	return _live;
}

/** Build the unit's input and slot on `ctx`; it becomes the live insert. */
export function buildBeatFxInsert(ctx: BaseAudioContext, masterGain: AudioNode): BeatFxInsert {
	_live = {
		ctx,
		masterGain,
		unitIn: ctx.createGain(),
		sends: new Map(),
		drys: new Map(),
		slot: null
	};
	return _live;
}

/** Forget the live insert (the graph was built with the flag off). */
export function clearLiveBeatFxInsert(): void {
	_live = null;
}

/** Hang one channel off the unit after its fader; returns the dry node the caller routes on. */
export function wireBeatFxChannel(insert: BeatFxInsert, deck: DeckId, fader: AudioNode): GainNode {
	const dry = insert.ctx.createGain();
	const send = insert.ctx.createGain();
	send.gain.value = 0;
	fader.connect(dry);
	fader.connect(send);
	send.connect(insert.unitIn);
	insert.sends.set(deck, send);
	insert.drys.set(deck, dry);
	return dry;
}

/** Put `kernel` in the slot for `effect`: the old kernel is disposed, the new one reads unitIn and returns to masterGain. */
export function setBeatFxKernel(insert: BeatFxInsert, effect: string, kernel: BeatFxKernel): void {
	insert.slot?.kernel.dispose();
	insert.unitIn.disconnect();
	insert.unitIn.connect(kernel.input);
	kernel.output.connect(insert.masterGain);
	insert.slot = { effect, kernel };
}

/** Whether `target` takes channel `deck` (master takes every channel). */
export function beatFxTargetTakes(target: BeatFxTarget, deck: DeckId): boolean {
	return target === 'master' || target === String(deck);
}

/** Drive the graph from unit state at context time `at`: kernel, timing, depth, then the 12 ms send ramps. */
export function syncBeatFxInsert(insert: BeatFxInsert, state: BeatFxUnitState, at: number): void {
	const start = Math.max(at, insert.ctx.currentTime);
	const open = state.enabled && state.level > 0;
	if (open && insert.slot?.effect !== state.effect) {
		const create = beatFxEffectDef(state.effect).createKernel;
		if (create === undefined)
			throw new TypeError(`beat_fx effect ${state.effect} has no audio kernel`);
		setBeatFxKernel(insert, state.effect, create(insert.ctx));
	}
	if (insert.slot !== null) {
		const timingMs = beatFxTimingMs(state);
		if (timingMs !== null) insert.slot.kernel.setTiming(timingMs / 1000, start);
		insert.slot.kernel.setDepth(state.level, start);
	}
	for (const [deck, send] of insert.sends) {
		scheduleGainStep(send.gain, open && beatFxTargetTakes(state.target, deck) ? 1 : 0, start);
	}
}
