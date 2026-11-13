/**
 * Edge sampler perf round 2 (EDGESAMP-11872i): the pure rules that decide warm re-use vs a cold rebuild.
 * No Web Audio here, so node tests can pin them.
 */

/** What a parked voice knows about itself when a trigger asks to re-use it. */
export interface WarmVoiceState<C, B, D> {
	warm: boolean;
	disposed: boolean;
	/** true while a trigger owns the voice (prepared or re-armed, not yet stopped) */
	armed: boolean;
	hasProcessor: boolean;
	ctx: C;
	loaded: B | null;
	destination: D;
}

/** True when the parked voice can replay `buffer` on `ctx` (and `destination`, when given) warm. */
export function canReuseWarm<C, B, D>(
	state: WarmVoiceState<C, B, D>,
	ctx: C,
	buffer: B,
	destination?: D
): boolean {
	return (
		(destination === undefined || destination === state.destination) &&
		state.warm &&
		!state.disposed &&
		!state.armed &&
		state.hasProcessor &&
		state.ctx === ctx &&
		state.loaded === buffer
	);
}

/** A free voice already holding the clip first, else the first free voice, else undefined. */
export function pickFreeVoice(
	ids: readonly number[],
	isLive: (id: number) => boolean,
	holdsClip: (id: number) => boolean
): number | undefined {
	return ids.find((id) => !isLive(id) && holdsClip(id)) ?? ids.find((id) => !isLive(id));
}

/** Throws when a launch at `at` would land inside the previous stop ramp (ends `idleAt`). */
export function assertLaunchClearsStopRamp(at: number, idleAt: number): void {
	if (at < idleAt) throw new Error(`voice start ${at} inside its stop ramp (ends ${idleAt})`);
}
