/** Shared fixed-position placement for interactive column-header explainers. */
export type ColumnExplainerPlacement = 'above' | 'below';

interface ColumnExplainerOptions {
	/** The same complete explanation that was formerly exposed only by `title`. */
	text: string;
	/** Headers teach above the table unless that would leave the viewport. */
	placement?: ColumnExplainerPlacement;
}

const PANEL_GAP_PX = 6;
const VIEWPORT_MARGIN_PX = 8;
const PANEL_Z_INDEX = 100;

function _clamp(value: number, lower: number, upper: number): number {
	return Math.max(lower, Math.min(value, Math.max(lower, upper)));
}

export function columnExplainerStyle(
	rect: Pick<DOMRect, 'left' | 'top' | 'bottom'>,
	panel: Pick<DOMRect, 'width' | 'height'>,
	viewport: Pick<Window, 'innerWidth' | 'innerHeight'>,
	placement: ColumnExplainerPlacement
): string {
	const left = _clamp(
		rect.left - panel.width - PANEL_GAP_PX,
		VIEWPORT_MARGIN_PX,
		viewport.innerWidth - panel.width - VIEWPORT_MARGIN_PX
	);
	const above = rect.top - panel.height - PANEL_GAP_PX;
	const below = rect.bottom + PANEL_GAP_PX;
	const aboveWouldClip = above < VIEWPORT_MARGIN_PX;
	const belowFits = below + panel.height <= viewport.innerHeight - VIEWPORT_MARGIN_PX;
	const desiredTop = placement === 'above' && aboveWouldClip && belowFits ? below : placement === 'above' ? above : below;
	const top = _clamp(
		desiredTop,
		VIEWPORT_MARGIN_PX,
		viewport.innerHeight - panel.height - VIEWPORT_MARGIN_PX
	);
	return `left:${Math.round(left)}px;top:${Math.round(top)}px;z-index:${PANEL_Z_INDEX};`;
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
	let options = initial;
	let panel: HTMLDivElement | null = null;
	const previousTabIndex = node.getAttribute('tabindex');
	const previousLabel = node.getAttribute('aria-label');
	const previousDescription = node.getAttribute('aria-describedby');

	function restoreDescription(): void {
		if (previousDescription === null) node.removeAttribute('aria-describedby');
		else node.setAttribute('aria-describedby', previousDescription);
	}

	function close(): void {
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

	function show(): void {
		if (panel !== null) return;
		const next = document.createElement('div');
		next.id = `column-explainer-${nextExplainerId++}`;
		next.className = 'column-explain-panel';
		next.setAttribute('role', 'tooltip');
		next.textContent = options.text;
		// Portal inside the nearest .perf-root ancestor (falling back to body
		// when none exists) rather than always document.body: --rb-border/
		// --rb-text/--rb-panel-raised are scoped to .perf-root (light/dark
		// palette swap lives there), and position:fixed still measures from
		// the viewport regardless of DOM parent since no ancestor here sets
		// a transform/filter/perspective containing block.
		(node.closest('.perf-root') ?? document.body).appendChild(next);
		panel = next;
		node.setAttribute('aria-describedby', next.id);
		place();
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
			options = next;
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
