import { OJ_GESTURE_MAX_MS, OJ_GESTURE_MIN_MS, ojMotionAnimates } from './oj-glass-motion';
import type { OjMotion } from './oj-motion-policy';

export type OjGlassState = 'idle' | 'smile' | 'slosh' | 'think';
export type OjGesture = 'smile' | 'slosh';

/** Length of one gesture; equals the CSS animation duration (1.2s). Must be >= OJ_MIN_TIMER_MS. */
export const OJ_GESTURE_MS = 1_200;

/** 20 000 + random() * 20 000, clamped to [OJ_GESTURE_MIN_MS, OJ_GESTURE_MAX_MS]. */
export function nextGestureDelayMs(random: () => number = Math.random): number {
	const span = OJ_GESTURE_MAX_MS - OJ_GESTURE_MIN_MS;
	const raw = OJ_GESTURE_MIN_MS + random() * span;
	return Math.min(OJ_GESTURE_MAX_MS, Math.max(OJ_GESTURE_MIN_MS, Math.round(raw)));
}

/** random() < 0.5 ? 'smile' : 'slosh'. */
export function pickGesture(random: () => number = Math.random): OjGesture {
	return random() < 0.5 ? 'smile' : 'slosh';
}

/**
 * Root classes. Always 'oj-glass' and the state name. Adds `gesture-${gesture}` only when
 * state === 'idle' && policy.idleGestures && gesture !== null. Adds 'still' when the policy fill is not animated.
 */
export function ojGlassClasses(
	state: OjGlassState,
	gesture: OjGesture | null,
	policy: OjMotion
): string[] {
	const classes = ['oj-glass', state];
	if (!ojMotionAnimates(policy)) {
		classes.push('still');
	}
	if (state === 'idle' && policy.idleGestures && gesture !== null) {
		classes.push(`gesture-${gesture}`);
	}
	return classes;
}

export type GestureScheduler = { start(): void; stop(): void };

export function createGestureScheduler(options: {
	getState: () => OjGlassState;
	getPolicy: () => OjMotion;
	onGesture: (gesture: OjGesture | null) => void;
	random?: () => number;
}): GestureScheduler {
	const random = options.random ?? Math.random;
	let gestureTimer: ReturnType<typeof setTimeout> | undefined;
	let clearTimer: ReturnType<typeof setTimeout> | undefined;
	let running = false;

	function clearAll(): void {
		const clear = globalThis.clearTimeout;
		if (gestureTimer !== undefined) {
			clear(gestureTimer);
			gestureTimer = undefined;
		}
		if (clearTimer !== undefined) {
			clear(clearTimer);
			clearTimer = undefined;
		}
	}

	function armNextGesture(): void {
		if (!running) return;
		const set = globalThis.setTimeout;
		gestureTimer = set(() => {
			gestureTimer = undefined;
			if (!running) return;
			if (options.getState() === 'idle' && options.getPolicy().idleGestures) {
				const g = pickGesture(random);
				options.onGesture(g);
				clearTimer = set(() => {
					clearTimer = undefined;
					options.onGesture(null);
					armNextGesture();
				}, OJ_GESTURE_MS);
			} else {
				armNextGesture();
			}
		}, nextGestureDelayMs(random));
	}

	return {
		start() {
			if (running) {
				this.stop();
			}
			running = true;
			armNextGesture();
		},
		stop() {
			running = false;
			clearAll();
		}
	};
}
