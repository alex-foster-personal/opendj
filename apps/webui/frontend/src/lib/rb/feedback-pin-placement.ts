import type { AnchorishElement } from './feedback';

/** Pin-system chrome that must not become the stored anchor. */
const PIN_SYSTEM_CLASSES = new Set([
	'fb-place-overlay',
	'fb-pin',
	'fb-bubble',
	'fb-pin-body',
	'fb-pin-badge',
	'fb-cluster',
	'fb-place-skip',
	'fb-shell-pin'
]);

function hasPinSystemClass(el: Element): boolean {
	for (const cls of el.classList) {
		if (PIN_SYSTEM_CLASSES.has(cls)) return true;
	}
	return false;
}

function isSkippableRoot(el: Element): boolean {
	const tag = el.tagName;
	return tag === 'HTML' || tag === 'BODY';
}

function isInsideFloatingSurface(el: Element): boolean {
	if (el.closest('.pop') !== null) return true;
	if (el.closest('[role="dialog"]') !== null) return true;
	if (el.closest('[data-overlay-z]') !== null) return true;
	return false;
}

const PIN_SYSTEM_SELECTOR = [...PIN_SYSTEM_CLASSES].map((cls) => `.${cls}`).join(',');

/** Pin markers, bubbles and the placement overlay: never a pin's anchor or
 * a nearby anchor, or pins would end up positioned against each other. */
export function isPinSystemElement(el: Element): boolean {
	return el.closest(PIN_SYSTEM_SELECTOR) !== null;
}

function isSkipped(el: Element): boolean {
	return hasPinSystemClass(el) || isSkippableRoot(el);
}

/**
 * Resolve the element a comment pin should anchor to at a viewport point.
 * Prefers floating surfaces (ControlExplainer pop, root overlays, dialogs)
 * over the main route beneath them.
 */
export function resolvePinAnchorAt(
	x: number,
	y: number,
	elementsFromPoint: (x: number, y: number) => Element[]
): AnchorishElement | null {
	const stack = elementsFromPoint(x, y);
	let fallback: Element | null = null;

	for (const el of stack) {
		if (isSkipped(el)) continue;
		if (isInsideFloatingSurface(el)) return el as AnchorishElement;
		if (fallback === null) fallback = el;
	}

	return (fallback as AnchorishElement | null) ?? null;
}
