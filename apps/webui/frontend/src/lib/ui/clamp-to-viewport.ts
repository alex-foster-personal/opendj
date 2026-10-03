/** UX-FLOAT-01: shared viewport clamp for trigger-relative floating surfaces. */

export const VIEWPORT_MARGIN_PX = 8;
export const DEFAULT_FLOAT_GAP_PX = 4;

export type FloatingPlacement = 'below' | 'above' | 'right' | 'left';

export interface Size {
	width: number;
	height: number;
}

export interface ViewportSize {
	width: number;
	height: number;
}

export interface TriggerRect {
	left: number;
	top: number;
	width: number;
	height: number;
}

export interface FloatingBox {
	x: number;
	y: number;
}

const OPPOSITE: Record<FloatingPlacement, FloatingPlacement> = {
	below: 'above',
	above: 'below',
	right: 'left',
	left: 'right'
};

function _proposedPosition(
	trigger: TriggerRect,
	size: Size,
	placement: FloatingPlacement,
	gap: number
): FloatingBox {
	const triggerRight = trigger.left + trigger.width;
	const triggerBottom = trigger.top + trigger.height;
	switch (placement) {
		case 'below':
			return { x: trigger.left, y: triggerBottom + gap };
		case 'above':
			return { x: trigger.left, y: trigger.top - size.height - gap };
		case 'right':
			return { x: triggerRight + gap, y: trigger.top };
		case 'left':
			return { x: trigger.left - size.width - gap, y: trigger.top };
	}
}

/** Overflow along the placement's OWN axis. The cross axis never decides a
 * flip: the clamp fixes it by sliding, without moving the box onto its trigger. */
function _overflows(
	pos: FloatingBox,
	size: Size,
	viewport: ViewportSize,
	margin: number,
	placement: FloatingPlacement
): boolean {
	if (placement === 'below' || placement === 'above') {
		return pos.y < margin || pos.y + size.height > viewport.height - margin;
	}
	return pos.x < margin || pos.x + size.width > viewport.width - margin;
}

/** Shift a proposed top-left so the full box stays inside the viewport inset by margin. */
export function clampToViewport(
	x: number,
	y: number,
	size: Size,
	viewport: ViewportSize,
	margin: number = VIEWPORT_MARGIN_PX
): FloatingBox {
	const maxX = Math.max(margin, viewport.width - size.width - margin);
	const maxY = Math.max(margin, viewport.height - size.height - margin);
	return {
		x: Math.min(Math.max(x, margin), maxX),
		y: Math.min(Math.max(y, margin), maxY)
	};
}

/** Prefer `preferred` relative to `trigger`, flip when that side overflows, then clamp. */
export function placeFloating(input: {
	trigger: TriggerRect;
	size: Size;
	viewport: ViewportSize;
	preferred?: FloatingPlacement;
	gap?: number;
	margin?: number;
}): FloatingBox {
	const {
		trigger,
		size,
		viewport,
		preferred = 'below',
		gap = DEFAULT_FLOAT_GAP_PX,
		margin = VIEWPORT_MARGIN_PX
	} = input;

	let pos = _proposedPosition(trigger, size, preferred, gap);
	if (_overflows(pos, size, viewport, margin, preferred)) {
		const flipped = _proposedPosition(trigger, size, OPPOSITE[preferred], gap);
		if (!_overflows(flipped, size, viewport, margin, OPPOSITE[preferred])) {
			pos = flipped;
		}
	}
	return clampToViewport(pos.x, pos.y, size, viewport, margin);
}

export interface TriggerFloatingOptions {
	getTrigger: () => HTMLElement | null;
	preferred?: FloatingPlacement;
	gap?: number;
}

/** Keep a fixed node clamped to its trigger after mount and on resize. */
export function triggerFloatingAction(
	node: HTMLElement,
	options: TriggerFloatingOptions
): { update: (next: TriggerFloatingOptions) => void; destroy: () => void } {
	let config = options;

	function place(): void {
		const trigger = config.getTrigger();
		if (trigger === null) return;
		const triggerRect = trigger.getBoundingClientRect();
		const width = node.offsetWidth;
		const height = node.offsetHeight;
		if (width <= 0 || height <= 0) return;
		const box = placeFloating({
			trigger: {
				left: triggerRect.left,
				top: triggerRect.top,
				width: triggerRect.width,
				height: triggerRect.height
			},
			size: { width, height },
			viewport: { width: window.innerWidth, height: window.innerHeight },
			preferred: config.preferred ?? 'below',
			gap: config.gap ?? DEFAULT_FLOAT_GAP_PX
		});
		node.style.position = 'fixed';
		node.style.left = `${Math.round(box.x)}px`;
		node.style.top = `${Math.round(box.y)}px`;
	}

	const ro = new ResizeObserver(() => place());
	ro.observe(node);
	const onViewportChange = (): void => place();
	window.addEventListener('resize', onViewportChange);
	window.addEventListener('scroll', onViewportChange, true);
	requestAnimationFrame(() => place());

	return {
		update(next: TriggerFloatingOptions): void {
			config = next;
			place();
		},
		destroy(): void {
			ro.disconnect();
			window.removeEventListener('resize', onViewportChange);
			window.removeEventListener('scroll', onViewportChange, true);
		}
	};
}

export interface ViewportFloatingPopoverOptions {
	preferred?: FloatingPlacement;
	gap?: number;
}

/** Positions a popover with `position: fixed` relative to its parent trigger. */
export function viewportFloatingPopover(
	popover: HTMLElement,
	options: ViewportFloatingPopoverOptions = {}
): { destroy: () => void } {
	const triggerEl = popover.parentElement;
	if (triggerEl === null) return { destroy: () => {} };

	const preferred = options.preferred ?? 'above';
	const gap = options.gap ?? DEFAULT_FLOAT_GAP_PX;

	function place(): void {
		if (triggerEl === null) return;
		const triggerRect = triggerEl.getBoundingClientRect();
		const width = popover.offsetWidth;
		const height = popover.offsetHeight;
		if (width <= 0 || height <= 0) return;
		const box = placeFloating({
			trigger: {
				left: triggerRect.left,
				top: triggerRect.top,
				width: triggerRect.width,
				height: triggerRect.height
			},
			size: { width, height },
			viewport: { width: window.innerWidth, height: window.innerHeight },
			preferred,
			gap
		});
		popover.style.position = 'fixed';
		popover.style.left = `${Math.round(box.x)}px`;
		popover.style.top = `${Math.round(box.y)}px`;
		popover.style.right = 'auto';
		popover.style.bottom = 'auto';
	}

	const onShow = (): void => {
		requestAnimationFrame(place);
	};

	triggerEl.addEventListener('pointerenter', onShow);
	triggerEl.addEventListener('focusin', onShow);
	window.addEventListener('resize', place);

	return {
		destroy(): void {
			triggerEl.removeEventListener('pointerenter', onShow);
			triggerEl.removeEventListener('focusin', onShow);
			window.removeEventListener('resize', place);
		}
	};
}
