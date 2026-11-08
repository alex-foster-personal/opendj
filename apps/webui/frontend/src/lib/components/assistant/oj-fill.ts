/**
 * Maps assistant reply phase and streamed character count to a juice fill
 * level (0..1) for the setup sidebar. Pure maths, no DOM or timers.
 */

export type OjFillPhase = 'idle' | 'waiting' | 'streaming' | 'done';
/** O4's motion-policy `fillMode` (#9474); duplicated here until that module lands. */
export type OjFillMode = 'animated' | 'static' | 'none';

export const OJ_FILL_WAITING = 0.3;
export const OJ_FILL_MAX = 0.9;
export const OJ_FILL_DRAIN_MS = 600;
/** Characters over which streaming covers ~63% of the gap from 0.3 to 0.9. */
export const OJ_FILL_CHAR_SCALE = 600;

export interface OjFillInput {
	phase: OjFillPhase;
	streamedChars: number;
	/** ms since the phase began; only `done` reads it. Omitted = settled (the CSS
	 * transition target), so `done` returns 0. */
	elapsedMs?: number;
}

function clampChars(chars: number): number {
	if (Number.isNaN(chars)) return 0;
	return Math.max(0, chars);
}

export function ojStreamingLevel(chars: number): number {
	const n = clampChars(chars);
	const gap = OJ_FILL_MAX - OJ_FILL_WAITING;
	return Math.min(OJ_FILL_MAX, OJ_FILL_WAITING + gap * (1 - Math.exp(-n / OJ_FILL_CHAR_SCALE)));
}

export function ojFillLevel(input: OjFillInput): number {
	const chars = clampChars(input.streamedChars);
	const { phase, elapsedMs } = input;
	switch (phase) {
		case 'idle':
			return 0;
		case 'waiting':
			return OJ_FILL_WAITING;
		case 'streaming':
			return ojStreamingLevel(chars);
		case 'done':
			if (elapsedMs === undefined) return 0;
			return ojStreamingLevel(chars) * Math.max(0, 1 - elapsedMs / OJ_FILL_DRAIN_MS);
		default: {
			const _exhaustive: never = phase;
			throw new Error(`unknown OJ fill phase: ${String(_exhaustive)}`);
		}
	}
}

export function ojFillTransitionMs(phase: OjFillPhase): number {
	switch (phase) {
		case 'idle':
			return 0;
		case 'waiting':
			return 400;
		case 'streaming':
			return 300;
		case 'done':
			return OJ_FILL_DRAIN_MS;
		default: {
			const _exhaustive: never = phase;
			throw new Error(`unknown OJ fill phase: ${String(_exhaustive)}`);
		}
	}
}

export function ojFillShowsLayer(mode: OjFillMode): boolean {
	switch (mode) {
		case 'animated':
			return true;
		case 'static':
			return true;
		case 'none':
			return false;
		default: {
			const _exhaustive: never = mode;
			throw new Error(`unknown OJ fill mode: ${String(_exhaustive)}`);
		}
	}
}

export function ojFillShowsBubbles(mode: OjFillMode, phase: OjFillPhase): boolean {
	if (mode !== 'animated') return false;
	return phase === 'waiting' || phase === 'streaming';
}
