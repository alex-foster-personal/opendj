/**
 * Keyboard selector focus restoration (#13428, epic #9372).
 *
 * A re-render (a keyed list refresh, the deck LESS/MORE swap that moves a channel's trim and
 * fader blocks) replaces the element that held focus, and the browser drops focus to <body>.
 * Focus is remembered by selector-tree identity, not by element: the innermost `data-sel-node`
 * id, its ancestor ids (innermost first) and the control's slot among that node's controls.
 * After each registry batch (`onSelectorRegistryChange`), when the focused element has left the
 * document and nothing else has taken focus, focus returns, in this order (the safe fallback):
 * 1. the node survived: the same control slot in its new element, else its primary control;
 * 2. the node is gone: the first non-text control of its nearest surviving ancestor (a text
 *    field would swallow the W A S D keys, so a fallback never lands on one);
 * 3. no ancestor survived: nowhere, focus stays on <body>. The selection ring still moves to the
 *    first root (#12256); focus does not jump into an unrelated section.
 * "Surviving" means present in the selector snapshot, so a disabled or hidden node does not count.
 * Focus on a control outside every selector node is never tracked, so it is never moved or restored,
 * and a deliberate blur (the focused element still in the page) cancels any restore.
 */
import { SELECTOR_CONTROL, selectorLeafPrimaryControl } from './selector-activate';
import {
	onSelectorRegistryChange,
	selectorNodeElement,
	snapshotSelectorNodes
} from './selector-register';
import { isTextEntryTarget } from './text-entry-target';

//----- pure core

/** Where focus sat in the selector tree: node id then ancestor ids, and the control slot (-1: none). */
export type SelectorFocusMark = { chain: readonly string[]; slot: number };

/** The node focus returns to: the marked node when present, else its nearest present ancestor. */
export function selectorFocusRestoreNode(
	mark: SelectorFocusMark,
	isPresent: (id: string) => boolean
): { id: string; survived: boolean } | null {
	const id = mark.chain.find(isPresent);
	if (id === undefined) return null;
	return { id, survived: id === mark.chain[0] };
}

//----- DOM shell

const NODE = '[data-sel-node]';

/** Where `target` sits in the selector tree, or null when no selector node contains it. */
export function markSelectorFocus(target: Element): SelectorFocusMark | null {
	const node = target.closest(NODE);
	if (node === null) return null;
	const chain: string[] = [];
	for (let el: Element | null = node; el !== null; el = el.parentElement?.closest(NODE) ?? null) {
		chain.push(el.getAttribute('data-sel-node')!);
	}
	return { chain, slot: Array.from(node.querySelectorAll(SELECTOR_CONTROL)).indexOf(target) };
}

/** Focus the control `mark` names in the current page; the control focused, or null for none. */
export function restoreSelectorFocus(
	mark: SelectorFocusMark,
	findNode: (id: string) => Element | null
): HTMLElement | null {
	const target = selectorFocusRestoreNode(mark, (id) => findNode(id) !== null);
	if (target === null) return null;
	const node = findNode(target.id)!;
	const controls = Array.from(node.querySelectorAll<HTMLElement>(SELECTOR_CONTROL));
	let control: HTMLElement | null;
	if (target.survived) {
		control = controls[mark.slot] ?? selectorLeafPrimaryControl(node);
	} else {
		control = controls.find((candidate) => !isTextEntryTarget(candidate)) ?? null;
	}
	control?.focus();
	return control;
}

/** The selector nodes present now (in the snapshot), by their current element. */
function _findPresentNode(): (id: string) => Element | null {
	const present = new Set(snapshotSelectorNodes().map((node) => node.id));
	return (id) => (present.has(id) ? selectorNodeElement(id) : null);
}

/** Track focus by selector identity and restore it after re-renders; returns the uninstaller. */
export function installSelectorFocusRestore(): () => void {
	let focused: { el: Element; mark: SelectorFocusMark } | null = null;
	const onFocusIn = (e: FocusEvent): void => {
		const mark = e.target instanceof Element ? markSelectorFocus(e.target) : null;
		focused = mark === null ? null : { el: e.target as Element, mark };
	};
	const onFocusOut = (e: FocusEvent): void => {
		const el = e.target;
		// After the blur settles: still in the page means a deliberate blur, not a re-render.
		queueMicrotask(() => {
			if (focused?.el === el && focused.el.isConnected) focused = null;
		});
	};
	const onRegistryChange = (): void => {
		if (focused === null || focused.el.isConnected) return;
		const { mark } = focused;
		focused = null;
		const active = document.activeElement;
		if (active !== null && active !== document.body) return;
		restoreSelectorFocus(mark, _findPresentNode());
	};
	window.addEventListener('focusin', onFocusIn, { capture: true });
	window.addEventListener('focusout', onFocusOut, { capture: true });
	const stopRegistry = onSelectorRegistryChange(onRegistryChange);
	return () => {
		stopRegistry();
		window.removeEventListener('focusin', onFocusIn, { capture: true });
		window.removeEventListener('focusout', onFocusOut, { capture: true });
	};
}
