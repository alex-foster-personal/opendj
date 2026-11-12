/**
 * OJ glass motion guard (issue #9475): gesture timing bounds for the OJ glass character;
 * the compositor-safety scan is main's oj-motion-policy.test.mjs. The motion decision itself is main's
 * ojMotionPolicy (oj-motion-policy.ts, #9474); this file only adds glass-specific limits.
 */
import type { OjMotion } from './oj-motion-policy';

export const OJ_GESTURE_MIN_MS = 20_000;
export const OJ_GESTURE_MAX_MS = 40_000;
/** Shortest JS timer OJ may arm (hard veto: no JS timers under 1 s). */
export const OJ_MIN_TIMER_MS = 1_000;

/** Full motion; used before the client has read the environment (SSR and first paint). */
export const OJ_MOTION_FULL: OjMotion = Object.freeze({
	idleGestures: true,
	bubbles: true,
	fillMode: 'animated'
});

/** CSS animations may run only when the policy keeps the fill animated. */
export function ojMotionAnimates(motion: OjMotion): boolean {
	return motion.fillMode === 'animated';
}
