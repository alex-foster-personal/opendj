/**
 * Generic hold-vs-press gesture primitive.
 *
 * LIBUX-05 ("Technically-working mode") needs Ctrl+R to TOGGLE (a quick press)
 * while also PEEKING (a sustained hold) - "hold = doesn't stay, press =
 * toggle". LIBUX-04 (hotkeys overlay) draws the identical
 * split across two separate keys ("/" holds, "?" toggles). Rather than wire
 * a bespoke timer per feature, this is the one mechanism both are meant to
 * share: give it a threshold and the two callbacks, and it tells you which
 * gesture happened.
 *
 * A press is keydown followed by keyup before `holdThresholdMs` elapses. A
 * hold is a keydown still down when the threshold elapses; `onHoldStart`
 * fires then (not on keydown), and `onHoldEnd` fires on the matching keyup.
 * Passing no `onPress` makes every keydown an immediate hold (LIBUX-05's Opt
 * gesture: there is no toggle reading, only "shown while held").
 */

export interface HoldOrPressOptions {
	/** Ignored when `onPress` is omitted (hold-only gestures fire immediately). */
	holdThresholdMs: number;
	onPress?: () => void;
	onHoldStart: () => void;
	onHoldEnd: () => void;
}

export interface HoldOrPressGesture {
	/** Call from the key's keydown handler. Ignores repeats while already down. */
	keyDown(): void;
	/** Call from the key's keyup handler. No-op if the key was not tracked down. */
	keyUp(): void;
	/**
	 * Abandon an in-flight keydown without firing onPress/onHoldEnd - for a
	 * lost keyup source (window blur) caught BEFORE the hold threshold
	 * resolved it either way. Once `holding` is true the gesture already
	 * committed to a hold and has open state to release (e.g. a peek/reveal
	 * still showing); resolve that case with `keyUp()` instead, or it sticks.
	 */
	cancel(): void;
	/** True between a tracked keydown and its resolving keyup. */
	readonly down: boolean;
	/** True once `onHoldStart` has fired for the current keydown. */
	readonly holding: boolean;
}

export function createHoldOrPress(options: HoldOrPressOptions): HoldOrPressGesture {
	const holdThresholdMs = options.onPress ? options.holdThresholdMs : 0;

	let timerId: ReturnType<typeof setTimeout> | null = null;
	let down = false;
	let holding = false;

	function keyDown(): void {
		if (down) return;
		down = true;
		holding = false;
		if (holdThresholdMs <= 0) {
			holding = true;
			options.onHoldStart();
			return;
		}
		timerId = setTimeout(() => {
			timerId = null;
			if (!down) return; // keyUp already resolved this as a press
			holding = true;
			options.onHoldStart();
		}, holdThresholdMs);
	}

	function keyUp(): void {
		if (!down) return;
		down = false;
		if (timerId !== null) {
			clearTimeout(timerId);
			timerId = null;
		}
		if (holding) {
			holding = false;
			options.onHoldEnd();
			return;
		}
		options.onPress?.();
	}

	function cancel(): void {
		if (!down) return;
		if (timerId !== null) {
			clearTimeout(timerId);
			timerId = null;
		}
		down = false;
		holding = false;
	}

	return {
		keyDown,
		keyUp,
		cancel,
		get down() {
			return down;
		},
		get holding() {
			return holding;
		}
	};
}
