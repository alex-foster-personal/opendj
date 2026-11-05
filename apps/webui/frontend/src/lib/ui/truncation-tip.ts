/**
 * A track name's hover tooltip, shown only when the name is cut off (LIBUX-57).
 *
 * the maintainer, Thu 5 Nov 2026: track names had a hover explainer that repeated the
 * name even when it fitted, placed by the OS, with almost no delay. This
 * action replaces the native `title` on a name label:
 *
 * - it shows only when the label is truncated, measured when the delay ends
 *   (`scrollWidth > clientWidth`), so a resize that makes a name fit or clip
 *   is honoured on the next hover;
 * - it opens after `TRUNCATION_TIP_DELAY_MS` of rest and a pointer leave
 *   cancels it, so sweeping across rows shows nothing;
 * - it sits just below the label, aligned with the text start, clamped into
 *   the viewport, in the app's own tooltip style (`.single-hover-tip`).
 *
 * The full name stays in the label's own text (the clip is visual only), so a
 * screen reader reads it whole; while shown, the tip also describes the label.
 * Put `data-custom-tip` on the label (or the cell around it) so the
 * document-wide title tooltip draws nothing there.
 *
 * Requirements (mini-PRD):
 *   ✔︎ A label that fits never shows a tip.
 *     [if] the text fits its box and the pointer rests past the delay [then] ⛔️ any tip shows
 *   ✔︎ A truncated label shows the full text after the delay.
 *     [if] the text is clipped and the pointer rests past the delay [then] ⛔️ no tip, or another string
 *   ✔︎ A quick pass shows nothing.
 *     [if] the pointer leaves before the delay ends [then] ⛔️ a tip shows
 */
import { placeFloating } from '$lib/ui/clamp-to-viewport';

export const TRUNCATION_TIP_DELAY_MS = 600;
export const TRUNCATION_TIP_ID = 'truncation-tip';
const TIP_GAP_PX = 4;

//------------------------------------------------------------------ helpers

let tipEl: HTMLDivElement | null = null;
let owner: HTMLElement | null = null;

function _ensureTip(): HTMLDivElement {
	if (tipEl !== null && tipEl.isConnected) return tipEl;
	const el = document.createElement('div');
	el.id = TRUNCATION_TIP_ID;
	el.className = 'single-hover-tip';
	el.setAttribute('role', 'tooltip');
	el.dataset.testid = 'truncation-tip';
	el.hidden = true;
	document.body.appendChild(el);
	tipEl = el;
	return el;
}

/** Whether the label's text is clipped right now. */
export function isTextTruncated(el: HTMLElement): boolean {
	return el.scrollWidth > el.clientWidth;
}

function _show(node: HTMLElement, text: string): void {
	const tip = _ensureTip();
	tip.textContent = text;
	tip.hidden = false;
	const rect = node.getBoundingClientRect();
	const box = placeFloating({
		trigger: { left: rect.left, top: rect.top, width: rect.width, height: rect.height },
		size: { width: tip.offsetWidth, height: tip.offsetHeight },
		viewport: { width: window.innerWidth, height: window.innerHeight },
		preferred: 'below',
		gap: TIP_GAP_PX
	});
	tip.style.left = `${Math.round(box.x)}px`;
	tip.style.top = `${Math.round(box.y)}px`;
	node.setAttribute('aria-describedby', TRUNCATION_TIP_ID);
	owner = node;
}

function _hide(node: HTMLElement): void {
	if (owner !== node) return;
	owner = null;
	node.removeAttribute('aria-describedby');
	if (tipEl === null) return;
	tipEl.hidden = true;
	tipEl.textContent = '';
}

//------------------------------------------------------------------ action

/**
 * `use:truncationTip` on a single-line, ellipsis-clipped label. The optional
 * parameter is the full text when it differs from what the label renders
 * (for example find-highlight marks split it); default is the label's text.
 */
export function truncationTip(node: HTMLElement, text?: string | null) {
	let fullText = text ?? null;
	let timer: ReturnType<typeof setTimeout> | null = null;

	// A scroll moves the label out from under a resting pointer without a leave.
	const cancel = (): void => {
		if (timer !== null) clearTimeout(timer);
		timer = null;
		document.removeEventListener('scroll', cancel, true);
		_hide(node);
	};
	const onEnter = (): void => {
		cancel();
		document.addEventListener('scroll', cancel, { capture: true, passive: true });
		timer = setTimeout(() => {
			timer = null;
			const shown = (fullText ?? node.textContent ?? '').trim();
			if (shown.length === 0 || !isTextTruncated(node)) return;
			_show(node, shown);
		}, TRUNCATION_TIP_DELAY_MS);
	};

	node.addEventListener('pointerenter', onEnter);
	node.addEventListener('pointerleave', cancel);
	node.addEventListener('pointerdown', cancel);
	return {
		update(next?: string | null): void {
			fullText = next ?? null;
		},
		destroy(): void {
			cancel();
			node.removeEventListener('pointerenter', onEnter);
			node.removeEventListener('pointerleave', cancel);
			node.removeEventListener('pointerdown', cancel);
		}
	};
}
