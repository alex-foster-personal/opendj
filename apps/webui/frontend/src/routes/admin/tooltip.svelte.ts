/**
 * Hover-explainer plumbing for the admin KPI panel.
 *
 * One floating tip layer, driven by a STACK of hovered elements. The stack is
 * what makes "hover anywhere on the card" work while finer targets inside the
 * card (sparkline dots) can still say something more specific: entering a dot
 * pushes its tip, leaving it pops back to the card's tip, because the pointer
 * is still inside the card and mouseleave never fired for it.
 *
 * Content is structured data, not HTML strings, so ledger-derived text is
 * rendered by Svelte and never interpolated into markup.
 */

export type TipTone = 'good' | 'bad' | 'flat';

export interface TipLine {
	text: string;
	tone?: TipTone;
}

export interface TipContent {
	/** Bold first line, e.g. the KPI name or a run label. */
	title: string;
	/** Dim line under the title, e.g. unit + direction. */
	subtitle?: string;
	/** Short coloured readouts, e.g. the delta versus the previous run. */
	lines?: TipLine[];
	/** Prose paragraphs: what the number means, why it matters. */
	body?: string[];
}

interface TipEntry {
	node: Element;
	content: TipContent;
}

const stack: TipEntry[] = [];

/** Topmost hovered element's tip, or null when nothing is hovered. */
export const tipState = $state<{ entry: TipEntry | null }>({ entry: null });

function sync(): void {
	tipState.entry = stack.length > 0 ? stack[stack.length - 1] : null;
}

function push(node: Element, content: TipContent): void {
	const existing = stack.findIndex((e) => e.node === node);
	if (existing >= 0) stack.splice(existing, 1);
	stack.push({ node, content });
	sync();
}

function pop(node: Element): void {
	const idx = stack.findIndex((e) => e.node === node);
	if (idx < 0) return;
	stack.splice(idx, 1);
	sync();
}

/**
 * Svelte action: show `content` while this element is hovered or focused.
 * Applied to the whole stat card, so the explainer comes up from anywhere on
 * it, and to the sparkline dots, which override it while pointed at.
 */
export function tip(node: HTMLElement | SVGElement, content: TipContent) {
	let current = content;
	const show = (): void => push(node, current);
	const hide = (): void => pop(node);

	node.addEventListener('mouseenter', show);
	node.addEventListener('mouseleave', hide);
	node.addEventListener('focus', show);
	node.addEventListener('blur', hide);

	return {
		update(next: TipContent): void {
			current = next;
			if (stack.some((e) => e.node === node)) push(node, next);
		},
		destroy(): void {
			pop(node);
			node.removeEventListener('mouseenter', show);
			node.removeEventListener('mouseleave', hide);
			node.removeEventListener('focus', show);
			node.removeEventListener('blur', hide);
		}
	};
}
