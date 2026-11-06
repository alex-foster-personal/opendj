import { clampToViewport, VIEWPORT_MARGIN_PX } from '$lib/ui/clamp-to-viewport';
import { OVERLAY_Z_INDEX } from '$lib/overlays/overlay-stack';
import { TOOLTIP_PACKED, type TooltipTiming } from '$lib/ui/tooltip-timing';

/** Shared fixed-position placement for interactive column-header explainers. */
export type ColumnExplainerPlacement = 'above' | 'below';

interface ColumnExplainerOptions {
	/** The same complete explanation that was formerly exposed only by `title`. */
	text: string;
	/** Headers teach above the table unless that would leave the viewport. */
	placement?: ColumnExplainerPlacement;
	tooltip?: TooltipTiming;
}

const PANEL_GAP_PX = 6;

export function columnExplainerStyle(
	rect: Pick<DOMRect, 'left' | 'top' | 'bottom'>,
	panel: Pick<DOMRect, 'width' | 'height'>,
	viewport: Pick<Window, 'innerWidth' | 'innerHeight'>,
	placement: ColumnExplainerPlacement
): string {
	const left = rect.left - panel.width - PANEL_GAP_PX;
	const above = rect.top - panel.height - PANEL_GAP_PX;
	const below = rect.bottom + PANEL_GAP_PX;
	const aboveWouldClip = above < VIEWPORT_MARGIN_PX;
	const belowFits = below + panel.height <= viewport.innerHeight - VIEWPORT_MARGIN_PX;
	const desiredTop =
		placement === 'above' && aboveWouldClip && belowFits
			? below
			: placement === 'above'
				? above
				: below;
	const box = clampToViewport(
		left,
		desiredTop,
		{ width: panel.width, height: panel.height },
		{ width: viewport.innerWidth, height: viewport.innerHeight }
	);
	return `left:${Math.round(box.x)}px;top:${Math.round(box.y)}px;z-index:${OVERLAY_Z_INDEX.tooltip};`;
}

let nextExplainerId = 0;

/**
 * Static header explanation with the same body portal and above-first placement
 * as the richer AutoPlay explainer. It deliberately observes only hover,
 * focus, and Escape, so sort clicks and resize pointer handling stay owned by
 * the header itself.
 */
export function columnExplainer(
	node: HTMLElement,
	initial: ColumnExplainerOptions
): { update: (next: ColumnExplainerOptions) => void; destroy: () => void } {
	let options = { tooltip: TOOLTIP_PACKED, ...initial };
	let panel: HTMLDivElement | null = null;
	let showTimer: ReturnType<typeof setTimeout> | undefined;
	const previousTabIndex = node.getAttribute('tabindex');
	const previousLabel = node.getAttribute('aria-label');
	const previousDescription = node.getAttribute('aria-describedby');

	function restoreDescription(): void {
		if (previousDescription === null) node.removeAttribute('aria-describedby');
		else node.setAttribute('aria-describedby', previousDescription);
	}

	function close(): void {
		if (showTimer !== undefined) clearTimeout(showTimer);
		showTimer = undefined;
		if (panel === null) return;
		panel.remove();
		panel = null;
		restoreDescription();
	}

	function place(): void {
		if (panel === null) return;
		panel.style.cssText = columnExplainerStyle(
			node.getBoundingClientRect(),
			panel.getBoundingClientRect(),
			window,
			options.placement ?? 'above'
		);
	}

	function openNow(): void {
		showTimer = undefined;
		if (panel !== null) return;
		const next = document.createElement('div');
		next.id = `column-explainer-${nextExplainerId++}`;
		next.className = 'column-explain-panel';
		next.setAttribute('role', 'tooltip');
		next.textContent = options.text;
		(node.closest('.perf-root') ?? document.body).appendChild(next);
		panel = next;
		node.setAttribute('aria-describedby', next.id);
		place();
	}

	function show(): void {
		if (showTimer !== undefined) clearTimeout(showTimer);
		showTimer = undefined;
		const timing = options.tooltip ?? TOOLTIP_PACKED;
		if (timing.delay > 0) {
			showTimer = setTimeout(openNow, timing.delay);
		} else {
			openNow();
		}
	}

	function updateAccessibility(): void {
		if (previousLabel === null) node.setAttribute('aria-label', options.text);
	}

	function onKeydown(event: KeyboardEvent): void {
		if (event.key !== 'Escape') return;
		event.preventDefault();
		close();
	}

	function onViewportChange(): void {
		place();
	}

	node.setAttribute('tabindex', '0');
	updateAccessibility();
	node.addEventListener('pointerenter', show);
	node.addEventListener('pointerleave', close);
	node.addEventListener('focusin', show);
	node.addEventListener('focusout', close);
	node.addEventListener('keydown', onKeydown);
	window.addEventListener('resize', onViewportChange);
	window.addEventListener('scroll', onViewportChange, true);

	return {
		update(next: ColumnExplainerOptions): void {
			options = { tooltip: TOOLTIP_PACKED, ...next };
			updateAccessibility();
			if (panel !== null) {
				panel.textContent = options.text;
				place();
			}
		},
		destroy(): void {
			close();
			node.removeEventListener('pointerenter', show);
			node.removeEventListener('pointerleave', close);
			node.removeEventListener('focusin', show);
			node.removeEventListener('focusout', close);
			node.removeEventListener('keydown', onKeydown);
			window.removeEventListener('resize', onViewportChange);
			window.removeEventListener('scroll', onViewportChange, true);
			if (previousTabIndex === null) node.removeAttribute('tabindex');
			else node.setAttribute('tabindex', previousTabIndex);
			if (previousLabel === null) node.removeAttribute('aria-label');
			else node.setAttribute('aria-label', previousLabel);
			restoreDescription();
		}
	};
}
