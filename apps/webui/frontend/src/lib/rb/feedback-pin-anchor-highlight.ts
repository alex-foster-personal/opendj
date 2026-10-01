const HIGHLIGHT_CLASS = 'fb-pin-anchor-target';
let highlighted: Element | null = null;

/** Selectors produced by describeAnchor only; refuse unknown shapes. */
function parseAnchorSelector(anchor: string): string | null {
	if (/^#[\w-]+$/.test(anchor)) return anchor;
	const testId = /^\[data-testid="([^"]+)"\]$/.exec(anchor);
	if (testId) return `[data-testid="${testId[1]}"]`;
	const aria = /^([a-z][\w-]*)\[aria-label="([^"]+)"\]$/.exec(anchor);
	if (aria) return `${aria[1]}[aria-label="${aria[2]}"]`;
	const cls = /^\.([\w-]+)$/.exec(anchor);
	if (cls) return `.${cls[1]}`;
	return null;
}

export function clearPinAnchorHighlight(): void {
	if (highlighted !== null) {
		highlighted.classList.remove(HIGHLIGHT_CLASS);
		highlighted = null;
	}
}

export function applyPinAnchorHighlight(anchor: string | null): void {
	clearPinAnchorHighlight();
	if (anchor === null || typeof document === 'undefined') return;
	const selector = parseAnchorSelector(anchor);
	if (selector === null) return;
	const el = document.querySelector(selector);
	if (el === null) return;
	el.classList.add(HIGHLIGHT_CLASS);
	highlighted = el;
}
