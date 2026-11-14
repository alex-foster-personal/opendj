/**
 * Operating the selected selector leaf (#13708, epic #9372).
 *
 * Selecting is not operating: once W A S D / E / Q sit on a leaf, Enter activates it and the
 * arrows adjust it. #14522: Shift+Enter presses the same control as a Shift+click (a `click`
 * MouseEvent with `shiftKey`), so a control's own Shift+click action (stem solo) is reachable,
 * and Enter on a leaf with no control but an adjust route (the jog) refuses with `no_control`. Enter clicks the leaf's primary control (the leaf itself when it is a
 * control, #14379, else its `data-sel-primary` element, else the first control in DOM order),
 * or focuses it when that control is a text input, select or slider. The arrows step a mixer
 * dial or fader leaf by the knob key step, through the same knob registry its own arrow keys
 * use. #14324: a leaf marked `data-sel-knob` steps that knob, a jog leaf nudges its deck's
 * tempo, and a leaf whose primary control is a slider gets the arrow forwarded to it as a
 * keydown, so the slider's own step and range rules apply.
 * Hygiene mimicked: WAI-ARIA APG slider pattern (Up/Right increase, Down/Left decrease, one
 * step per press, OS key repeat).
 *
 * No Svelte or knob-registry imports: strip-types unit tests load it directly.
 */
import type { DeckId } from '$lib/rb/deck-id';
import type { SelectorLeafCommandId, SelectorLeafHost } from './command-registry';
import { isTextEntryTarget } from './text-entry-target';

//----- pure core

const LEAF_KEYS: Readonly<Record<string, SelectorLeafCommandId>> = {
	Enter: 'selector.activate',
	ArrowUp: 'selector.adjust.up',
	ArrowRight: 'selector.adjust.up',
	ArrowDown: 'selector.adjust.down',
	ArrowLeft: 'selector.adjust.down'
};

/** The leaf command a key runs while a selector leaf is selected, or null when the key is not one. */
export function selectorLeafCommandForKey(
	key: string,
	shiftKey: boolean
): SelectorLeafCommandId | null {
	if (shiftKey) return key === 'Enter' ? 'selector.shift-activate' : null;
	return LEAF_KEYS[key] ?? null;
}

/** #14522: the leaf commands that press the control; a held key runs them once, not on repeat. */
export function isSelectorLeafActivation(command: SelectorLeafCommandId): boolean {
	return command === 'selector.activate' || command === 'selector.shift-activate';
}

/** #14522: why a leaf command did nothing, in the agent handle's refusal vocabulary. */
export type SelectorLeafRefusalCode = 'no_control';

const SELECTOR_LEAF_REFUSAL = 'SelectorLeafRefusal';

/** #14522: a leaf command that cannot act on this leaf by design; callers report it, never crash. */
export class SelectorLeafRefusal extends Error {
	readonly code: SelectorLeafRefusalCode;

	constructor(leafId: string, code: SelectorLeafRefusalCode) {
		super(`Selector leaf ${leafId} refused: ${code}`);
		this.name = SELECTOR_LEAF_REFUSAL;
		this.code = code;
	}
}

/** True for a SelectorLeafRefusal from any bundle copy of this module (unit tests load several). */
export function isSelectorLeafRefusal(error: unknown): error is SelectorLeafRefusal {
	return error instanceof Error && error.name === SELECTOR_LEAF_REFUSAL;
}

const CHANNEL_CONTROL = /^mixer\.ch(\d)\.(trim|high|mid|low|filter|fader)$/;
const CHANNEL_STEM = /^mixer\.ch(\d)\.stem\.(vocal|instrumental|drums|bass)$/;

/** The knob registry id (`deck:role`) behind a dial or fader leaf, or null when the leaf has none. */
export function selectorLeafControlId(leafId: string): string | null {
	const control = CHANNEL_CONTROL.exec(leafId);
	if (control !== null) return `${control[1]}:${control[2]}`;
	const stem = CHANNEL_STEM.exec(leafId);
	if (stem !== null) return `${stem[1]}:stem-${stem[2]}`;
	return null;
}

const JOG_LEAF = /^deck\.([1-4])\.jog(?:\.dial)?$/;

/** #14324: the deck whose tempo a jog leaf (`deck.<n>.jog` or `deck.<n>.jog.dial`) nudges, or null. */
export function selectorLeafJogDeck(leafId: string): DeckId | null {
	const jog = JOG_LEAF.exec(leafId);
	return jog === null ? null : (Number(jog[1]) as DeckId);
}

//----- DOM shell

export const SELECTOR_PRIMARY_ATTR = 'data-sel-primary';

/** Every control a selector leaf can be operated or focused through, in DOM order. */
export const SELECTOR_CONTROL = [
	'button:not([disabled])',
	'input:not([disabled])',
	'select:not([disabled])',
	'textarea:not([disabled])',
	'[role="button"]:not([aria-disabled="true"])',
	'[role="slider"]'
].join(', ');

/** Text entry and sliders take focus on Enter; anything else is pressed. */
function _takesFocus(control: HTMLElement): boolean {
	if (isTextEntryTarget(control) || control.getAttribute('role') === 'slider') return true;
	return control.tagName === 'INPUT' && control.getAttribute('type') === 'range';
}

/** #14379: a leaf registered on a control (marked primary, or itself an enabled control) is that control. */
function _leafIsOwnControl(leaf: Element): boolean {
	return leaf.getAttribute(SELECTOR_PRIMARY_ATTR) !== null || leaf.matches(SELECTOR_CONTROL);
}

/**
 * The control Enter operates and the arrows forward to in a leaf: the leaf itself when it is a
 * control (#14379), else its marked primary, else its first control in DOM order.
 */
export function selectorLeafPrimaryControl(leaf: Element): HTMLElement | null {
	if (_leafIsOwnControl(leaf)) return leaf as HTMLElement;
	return (
		leaf.querySelector<HTMLElement>(`[${SELECTOR_PRIMARY_ATTR}]`) ??
		leaf.querySelector<HTMLElement>(SELECTOR_CONTROL)
	);
}

/** One Shift+click on a control: a bubbling `click` MouseEvent carrying `shiftKey`. */
function _shiftClick(control: HTMLElement): void {
	const init = { bubbles: true, cancelable: true, composed: true, shiftKey: true };
	control.dispatchEvent(new MouseEvent('click', init));
}

/**
 * Enter on a leaf: click its primary control once, or focus it when it is an input or slider.
 * With `shift` (#14522) the click is a Shift+click; focus targets ignore it.
 */
export function activateSelectorLeaf(leaf: Element, shift = false): 'click' | 'focus' {
	const control = selectorLeafPrimaryControl(leaf);
	if (control === null) {
		const id = leaf.getAttribute('data-sel-node');
		throw new Error(`Selector leaf ${id} has no control to activate`);
	}
	if (_takesFocus(control)) {
		control.focus();
		return 'focus';
	}
	if (shift) _shiftClick(control);
	else control.click();
	return 'click';
}

/** What the leaf host needs from the page: the leaf element, the knob registry and the tempo nudge. */
export interface SelectorLeafHostDeps {
	findLeaf: (leafId: string) => Element | null;
	readControl: (controlId: string) => number | null;
	nudgeControl: (controlId: string, delta: number) => void;
	nudgeTempo: (deck: DeckId, direction: 1 | -1) => void;
	keyStep: number;
}

export const SELECTOR_KNOB_ATTR = 'data-sel-knob';

/** What the arrows on a leaf drive: a registered knob, a deck's tempo nudge or a slider control. */
export type SelectorLeafAdjustTarget =
	| { kind: 'knob'; controlId: string }
	| { kind: 'tempo'; deck: DeckId }
	| { kind: 'slider'; control: HTMLElement };

/** The knob id a leaf maps itself to with `data-sel-knob`, on the leaf or one element inside it. */
function _markedKnobId(leaf: Element): string | null {
	return (
		leaf.getAttribute(SELECTOR_KNOB_ATTR) ??
		leaf.querySelector(`[${SELECTOR_KNOB_ATTR}]`)?.getAttribute(SELECTOR_KNOB_ATTR) ??
		null
	);
}

/** #14324: what the arrows adjust on a leaf, or null when it has no knob, jog or slider behind it. */
export function selectorLeafAdjustTarget(
	leafId: string,
	deps: SelectorLeafHostDeps
): SelectorLeafAdjustTarget | null {
	const mapped = selectorLeafControlId(leafId);
	if (mapped !== null && deps.readControl(mapped) !== null)
		return { kind: 'knob', controlId: mapped };
	const deck = selectorLeafJogDeck(leafId);
	if (deck !== null) return { kind: 'tempo', deck };
	const leaf = deps.findLeaf(leafId);
	if (leaf === null) return null;
	const marked = _markedKnobId(leaf);
	if (marked !== null && deps.readControl(marked) !== null)
		return { kind: 'knob', controlId: marked };
	const control = selectorLeafPrimaryControl(leaf);
	if (control !== null && control.getAttribute('role') === 'slider')
		return { kind: 'slider', control };
	return null;
}

/** Marks a keydown the leaf host forwarded; a global symbol, so every bundle copy agrees. */
const FORWARDED_KEY = Symbol.for('opendj.selector.forwarded-key');

/** True for an arrow keydown the leaf host forwarded to a slider; the selector must let it through. */
export function isForwardedSelectorKey(event: object): boolean {
	return (event as Record<symbol, unknown>)[FORWARDED_KEY] === true;
}

/** One arrow keydown on a slider: Right/Left when it is horizontal, else Up/Down. */
function _forwardArrow(control: HTMLElement, direction: 1 | -1): void {
	const horizontal = control.getAttribute('aria-orientation') === 'horizontal';
	const up = horizontal ? 'ArrowRight' : 'ArrowUp';
	const down = horizontal ? 'ArrowLeft' : 'ArrowDown';
	const key = direction > 0 ? up : down;
	const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true });
	Object.defineProperty(event, FORWARDED_KEY, { value: true });
	control.dispatchEvent(event);
}

/** The selected leaf as the registry's leaf host: Enter presses its control, arrows step its dial. */
export function createSelectorLeafHost(
	leafId: string,
	deps: SelectorLeafHostDeps
): SelectorLeafHost {
	return {
		activate(shift): string {
			const leaf = deps.findLeaf(leafId);
			if (leaf === null) throw new Error(`Selector leaf ${leafId} is not in the page`);
			// #14522: a jog (or other adjust-only leaf) has nothing to press; the arrows operate it.
			const adjustOnly =
				selectorLeafPrimaryControl(leaf) === null &&
				selectorLeafAdjustTarget(leafId, deps) !== null;
			if (adjustOnly) throw new SelectorLeafRefusal(leafId, 'no_control');
			activateSelectorLeaf(leaf, shift);
			return leafId;
		},
		adjust(direction): string {
			const target = selectorLeafAdjustTarget(leafId, deps);
			if (target === null)
				throw new Error(`Selector leaf ${leafId} has no registered dial or fader to adjust`);
			switch (target.kind) {
				case 'knob':
					deps.nudgeControl(target.controlId, direction * deps.keyStep);
					break;
				case 'tempo':
					deps.nudgeTempo(target.deck, direction);
					break;
				case 'slider':
					_forwardArrow(target.control, direction);
					break;
				default: {
					const _exhaustive: never = target;
					throw new Error(`Unhandled adjust target: ${JSON.stringify(_exhaustive)}`);
				}
			}
			return leafId;
		}
	};
}
