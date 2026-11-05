/**
 * ANIM-02: which controls are animating right now, and the Animate box's state.
 *
 * Every AGENT-03 ramp (an agent's `ramp` order, `animate_control`, or the
 * right-click Animate box) registers here for as long as it runs, keyed by the
 * control's selection-registry id (`1:low`, `2:fader`, `crossfader`), so the
 * dial shows a running dot and a second animation on the same control replaces
 * the first instead of fighting it.
 *
 * Kept tiny on purpose: Knob and VFader import it on every page load.
 */
import type { AnimateControl } from './animate-expr';

type DeckId = 1 | 2 | 3 | 4;

export interface RunningAnimation {
	/** What the operator or agent asked for, shown as the dot's tooltip. */
	label: string;
}

export const dialAnimations = $state<{ running: Record<string, RunningAnimation> }>({ running: {} });

export const animatePrompt = $state<{
	open: boolean;
	deck: DeckId | null;
	control: AnimateControl | null;
	x: number;
	y: number;
}>({ open: false, deck: null, control: null, x: 0, y: 0 });

const _cancels = new Map<string, { token: symbol; cancel: (reason: string) => void }>();

/** Register a running animation. A previous one on the same control is cancelled first. */
export function beginAnimation(key: string, label: string, cancel: (reason: string) => void): symbol {
	_cancels.get(key)?.cancel('replaced by a newer animation on this control');
	const token = Symbol(key);
	_cancels.set(key, { token, cancel });
	dialAnimations.running[key] = { label };
	return token;
}

/** Drop a finished animation; a stale token (already replaced) changes nothing. */
export function endAnimation(key: string, token: symbol): void {
	if (_cancels.get(key)?.token !== token) return;
	_cancels.delete(key);
	delete dialAnimations.running[key];
}

/** Stop the animation on a control. True when one was running. */
export function cancelAnimation(key: string, reason: string): boolean {
	const entry = _cancels.get(key);
	if (entry === undefined) return false;
	entry.cancel(reason);
	return true;
}

export function openAnimatePrompt(deck: DeckId | null, control: AnimateControl, x: number, y: number): void {
	Object.assign(animatePrompt, { open: true, deck, control, x, y });
}

export function closeAnimatePrompt(): void {
	animatePrompt.open = false;
}
