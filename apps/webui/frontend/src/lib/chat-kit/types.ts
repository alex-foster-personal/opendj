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

/** Presentational tool-step status for ToolTrace (mid-stream, before `done`). */
export type ToolTraceStepStatus = 'running' | 'ok' | 'failed';

export const TOOL_TRACE_STEP_STATUSES: readonly ToolTraceStepStatus[] = ['running', 'ok', 'failed'];

export interface ToolStepRef {
	id: string;
	name: string;
	status: ToolTraceStepStatus;
}

export interface ChatKitComponents {
	CitationPill: Component<{ citation: CitationRef }>;
	StreamingBubble: Component<{
		text?: string;
		delta?: string;
		streaming?: boolean;
		role?: StreamingBubbleRole;
	}>;
	ToolTrace: Component<{
		steps?: ToolStepRef[];
		/** When true, the assistant stream is still open (no terminal `done` yet). */
		streaming?: boolean;
	}>;
}

export function toolTraceStepTitle(status: ToolTraceStepStatus): string {
	switch (status) {
		case 'running':
			return 'Tool running';
		case 'ok':
			return 'Tool finished';
		case 'failed':
			return 'Tool failed';
		default: {
			const _exhaustive: never = status;
			throw new Error(`unknown tool trace status: ${String(_exhaustive)}`);
		}
	}
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
