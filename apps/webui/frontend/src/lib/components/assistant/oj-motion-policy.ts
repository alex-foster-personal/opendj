/**
 * OJ motion policy (issue #9474, epic #9374 hard veto): OJ must not cost frame
 * time while music plays. Pure function, no imports, no timers.
 *
 * Rules, first match wins:
 *   1. reducedMotion            -> no gestures, no bubbles, fill static (macOS HIG Reduce Motion)
 *   2. anyDeckPlaying && performanceVisible -> no gestures, no bubbles, fill static
 *   3. otherwise                -> everything on
 * `streaming` is validated but does not change the output yet.
 */

export interface OjMotionInput {
	anyDeckPlaying: boolean;
	performanceVisible: boolean;
	reducedMotion: boolean;
	streaming: boolean;
}

export type OjFillMode = 'animated' | 'static' | 'none';

export interface OjMotion {
	idleGestures: boolean;
	bubbles: boolean;
	fillMode: OjFillMode;
}

const INPUT_KEYS = ['anyDeckPlaying', 'performanceVisible', 'reducedMotion', 'streaming'] as const;

const STILL: OjMotion = Object.freeze({ idleGestures: false, bubbles: false, fillMode: 'static' });
const LIVE: OjMotion = Object.freeze({ idleGestures: true, bubbles: true, fillMode: 'animated' });

export function ojMotionPolicy(input: OjMotionInput): OjMotion {
	for (const key of INPUT_KEYS) {
		if (typeof input?.[key] !== 'boolean') {
			throw new TypeError(`ojMotionPolicy: ${key} must be a boolean, got ${typeof input?.[key]}`);
		}
	}
	if (input.reducedMotion) return { ...STILL };
	if (input.anyDeckPlaying && input.performanceVisible) return { ...STILL };
	return { ...LIVE };
}

/** True when any deck in `deckStates` (lib/player/state.svelte.ts) is playing. */
export function anyDeckPlaying(decks: Record<string, { playing: boolean }>): boolean {
	return Object.entries(decks).some(([id, deck]) => {
		if (typeof deck?.playing !== 'boolean')
			throw new TypeError(`deck ${id}: playing must be a boolean`);
		return deck.playing;
	});
}
