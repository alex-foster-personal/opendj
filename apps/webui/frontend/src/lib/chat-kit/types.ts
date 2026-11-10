import type { Component } from 'svelte';

/** Verification state for a source citation pill (lifted from orbital-af-human-1). */
export type CitationState = 'verified' | 'warn' | 'fail';

export const CITATION_STATES: readonly CitationState[] = ['verified', 'warn', 'fail'];

export interface CitationRef {
	/** Short label shown on the pill (for example a domain or doc title). */
	label: string;
	/** When set, the pill is a link; otherwise it is inert text. */
	href?: string | null;
	state: CitationState;
	/** Hover title; a default names the state when omitted. */
	title?: string;
}

export type StreamingBubbleRole = 'assistant';

export interface ChatKitComponents {
	CitationPill: Component<{ citation: CitationRef }>;
	StreamingBubble: Component<{
		text?: string;
		delta?: string;
		streaming?: boolean;
		role?: StreamingBubbleRole;
	}>;
}

/** Append one streamed token delta without dropping prior partial text. */
export function appendTextDelta(text: string, delta: string): string {
	if (delta.length === 0) return text;
	return text + delta;
}

export function citationStateTitle(state: CitationState): string {
	switch (state) {
		case 'verified':
			return 'Source verified';
		case 'warn':
			return 'Source needs review';
		case 'fail':
			return 'Source could not be verified';
		default: {
			const _exhaustive: never = state;
			throw new Error(`unknown citation state: ${String(_exhaustive)}`);
		}
	}
}
