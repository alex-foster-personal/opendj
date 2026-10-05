/**
 * One hover tooltip for the desktop shell.
 *
 * Controls carry help on the HTML `title` attribute (the house rule for
 * numeric readouts, and the text WebKit shows as a native macOS tooltip).
 * This layer draws that same string in our own tooltip. While it is showing,
 * the live `title` is parked on `data-tip` so WKWebView does not draw a
 * second, native tooltip on top. Pointer leave puts `title` back.
 *
 * `apps/webui/frontend/src/lib/audio-engine/rust-inert.ts` watches `title`
 * and writes its own string back. It treats `data-tip-parked` as a temporary
 * park and does not record the missing attribute as the control's title.
 */
import { placeFloating } from '$lib/ui/clamp-to-viewport';

/** Set on an element whose live `title` is parked for the custom tooltip. */
export const TIP_PARKED_ATTR = 'data-tip-parked';

/** The parked help string. Screen readers also get it via `aria-describedby`. */
export const TIP_DATA_ATTR = 'data-tip';

/** Set on a control that already draws its own rich hover (ControlExplainer). */
export const CUSTOM_TIP_ATTR = 'data-custom-tip';

const TIP_ID = 'single-hover-tip';
const DESCRIBED_BACKUP_ATTR = 'data-tip-describedby';

let installed = false;
let tipEl: HTMLDivElement | null = null;
let active: HTMLElement | null = null;

function _element(node: EventTarget | null): HTMLElement | null {
	if (node instanceof HTMLElement) return node;
	if (node instanceof Element) return node.parentElement;
	return null;
}

function _titledHost(target: HTMLElement): HTMLElement | null {
	const found = target.closest(`[title], [${TIP_PARKED_ATTR}]`);
	if (!(found instanceof HTMLElement)) return null;
	if (found === tipEl) return null;
	return found;
}

function _text(el: HTMLElement): string | null {
	const live = el.getAttribute('title');
	if (live !== null && live.length > 0) return live;
	const parked = el.getAttribute(TIP_DATA_ATTR);
	if (parked !== null && parked.length > 0) return parked;
	return null;
}

function _ensureTip(): HTMLDivElement {
	if (tipEl !== null) return tipEl;
	const el = document.createElement('div');
	el.id = TIP_ID;
	el.className = 'single-hover-tip';
	el.setAttribute('role', 'tooltip');
	el.hidden = true;
	document.body.appendChild(el);
	tipEl = el;
	return el;
}

function _place(host: HTMLElement, tip: HTMLDivElement): void {
	const rect = host.getBoundingClientRect();
	const width = tip.offsetWidth || 160;
	const height = tip.offsetHeight || 24;
	const box = placeFloating({
		trigger: { left: rect.left, top: rect.top, width: rect.width, height: rect.height },
		size: { width, height },
		viewport: {
			width: window.innerWidth || 800,
			height: window.innerHeight || 600
		},
		preferred: 'below',
		gap: 4
	});
	tip.style.left = `${Math.round(box.x)}px`;
	tip.style.top = `${Math.round(box.y)}px`;
}

function _linkDescription(el: HTMLElement): void {
	if (el.getAttribute('aria-describedby')?.split(/\s+/).includes(TIP_ID)) return;
	if (!el.hasAttribute(DESCRIBED_BACKUP_ATTR)) {
		el.setAttribute(DESCRIBED_BACKUP_ATTR, el.getAttribute('aria-describedby') ?? '');
	}
	const prev = el.getAttribute(DESCRIBED_BACKUP_ATTR) ?? '';
	el.setAttribute('aria-describedby', prev.length > 0 ? `${prev} ${TIP_ID}` : TIP_ID);
}

function _unlinkDescription(el: HTMLElement): void {
	if (!el.hasAttribute(DESCRIBED_BACKUP_ATTR)) return;
	const prev = el.getAttribute(DESCRIBED_BACKUP_ATTR) ?? '';
	el.removeAttribute(DESCRIBED_BACKUP_ATTR);
	if (prev.length === 0) el.removeAttribute('aria-describedby');
	else el.setAttribute('aria-describedby', prev);
}

/** Park a live `title` so the native tooltip cannot show. Returns the text. */
export function parkHoverTitle(el: HTMLElement): string | null {
	const text = _text(el);
	if (text === null) return null;
	if (!el.hasAttribute(TIP_PARKED_ATTR)) {
		// The park flag goes on before `title` comes off. rust-inert's
		// MutationObserver then sees a parked control and does not treat the
		// cleared title as the component's new value, and does not write it back.
		el.setAttribute(TIP_DATA_ATTR, text);
		el.setAttribute(TIP_PARKED_ATTR, '');
		if (el.hasAttribute('title')) el.removeAttribute('title');
	}
	_linkDescription(el);
	return text;
}

/** Put the parked help back on `title` and drop the custom description. */
export function restoreHoverTitle(el: HTMLElement): void {
	if (!el.hasAttribute(TIP_PARKED_ATTR) && !el.hasAttribute(TIP_DATA_ATTR)) {
		_unlinkDescription(el);
		return;
	}
	const text = el.getAttribute(TIP_DATA_ATTR);
	el.removeAttribute(TIP_PARKED_ATTR);
	el.removeAttribute(TIP_DATA_ATTR);
	_unlinkDescription(el);
	if (text !== null) el.setAttribute('title', text);
}

function _hide(): void {
	if (tipEl === null) return;
	tipEl.hidden = true;
	tipEl.textContent = '';
}

function _show(host: HTMLElement, text: string, visible: boolean): void {
	const tip = _ensureTip();
	tip.hidden = false;
	tip.textContent = text;
	tip.classList.toggle('single-hover-tip-sr', !visible);
	if (visible) _place(host, tip);
}

function _activate(host: HTMLElement, showBox: boolean): void {
	const text = parkHoverTitle(host);
	if (text === null) return;
	active = host;
	// Inside a rich explainer the popover is the visible tooltip. The same
	// string still sits in this node, clipped, so aria-describedby has a target.
	_show(host, text, showBox);
}

function _clear(): void {
	const prev = active;
	active = null;
	_hide();
	if (prev !== null) restoreHoverTitle(prev);
}

function _onOver(event: Event): void {
	const target = _element(event.target);
	if (target === null || target === tipEl) return;
	const host = _titledHost(target);
	const showBox = target.closest(`[${CUSTOM_TIP_ATTR}]`) === null;
	if (host === null) {
		if (active !== null) _clear();
		return;
	}
	if (host === active) {
		if (!showBox) _hide();
		return;
	}
	_clear();
	_activate(host, showBox);
}

function _onOut(event: PointerEvent): void {
	if (active === null) return;
	const next = _element(event.relatedTarget);
	if (next !== null && (next === active || active.contains(next))) return;
	_clear();
}

/**
 * Install the document hover tooltip once. Returns a remover for tests and
 * for layout unmount.
 */
export function installSingleHoverTooltip(doc: Document = document): () => void {
	if (installed) return () => {};
	installed = true;
	doc.addEventListener('pointerover', _onOver, true);
	doc.addEventListener('pointerout', _onOut, true);
	return () => {
		doc.removeEventListener('pointerover', _onOver, true);
		doc.removeEventListener('pointerout', _onOut, true);
		_clear();
		tipEl?.remove();
		tipEl = null;
		installed = false;
	};
}
