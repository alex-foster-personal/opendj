/**
 * One rule for every collapse and expand in the library: sidebar folders,
 * playlist sections and version stacks (the maintainer, Tue 27 Oct 2026; row
 * "Collapse / expand" in the library-organization UI contract).
 *
 * - 100 ms.
 * - Only opacity and a `grid-template-rows: 0fr -> 1fr` wrapper animate, so
 *   the browser never re-lays-out row heights frame by frame. height, top and
 *   margin never animate.
 * - It snaps with no animation when the block holds more than 200 rows or
 *   the OS asks for reduced motion.
 *
 * Usage: the element carrying `in:collapse out:collapse` must be `display: grid`
 * with ONE child that has `min-height: 0; overflow: hidden` (the `.collapse`
 * / `.collapse-inner` pair). That child is what 0fr shrinks, and its direct
 * children are the rows counted against the 200-row snap, so a hint or
 * notice row rendered inside the block counts too (sol review P1, PR #7600).
 */
import type { TransitionConfig } from 'svelte/transition';

export const COLLAPSE_MS = 100;

/** Above this many rows the block snaps open or shut. */
export const COLLAPSE_SNAP_ROWS = 200;

/** 0 means snap. */
export function collapseDuration(rows: number, reducedMotion: boolean): number {
	if (!Number.isSafeInteger(rows) || rows < 0) {
		throw new RangeError(`collapse: rows must be a non-negative integer, got ${String(rows)}`);
	}
	if (reducedMotion || rows > COLLAPSE_SNAP_ROWS) return 0;
	return COLLAPSE_MS;
}

/** The frame at progress t (0 shut, 1 open): grid rows and opacity, nothing else. */
export function collapseCss(t: number): string {
	return `grid-template-rows: ${t}fr; opacity: ${t};`;
}

function _easeOutCubic(t: number): number {
	return 1 - (1 - t) ** 3;
}

function _prefersReducedMotion(): boolean {
	return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/** The rows a collapse block renders: the direct children of its one clipping
 * child. Anything else is a misuse of the transition, so it throws. */
export function collapseRowCount(node: Pick<Element, 'children'>): number {
	if (node.children.length !== 1) {
		throw new Error(
			`collapse: the transitioned element needs exactly one clipping child, found ${node.children.length}`
		);
	}
	return node.children[0].childElementCount;
}

/** Svelte transition for both directions, applied as `in:collapse out:collapse`.
 * Not `transition:collapse`: Svelte keeps a bidirectional transition's config
 * for the element's lifetime, so the row count and the reduced-motion
 * preference would be read only when the block opens. Separate in and out
 * directives call this again when the close begins (sol review P1, PR #7600). */
export function collapse(node: Element): TransitionConfig {
	return {
		duration: collapseDuration(collapseRowCount(node), _prefersReducedMotion()),
		easing: _easeOutCubic,
		css: collapseCss
	};
}
