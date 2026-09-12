/**
 * Pure classifier for unexpected playing-to-paused transitions (issue #2153).
 */

export type UnexpectedPauseCause =
	| 'context-suspended'
	| 'source-ended-early'
	| 'worklet-error'
	| 'autoplay-handoff-failed';

export type PauseOrigin = 'command' | 'natural-end' | 'unload' | 'dropout' | 'worklet' | 'other';

export const EARLY_END_SLACK_MS = 250;

const NON_RUNNING_CONTEXT = new Set(['suspended', 'interrupted', 'closed']);

export function diagnoseUnexpectedPause(input: {
	origin: PauseOrigin;
	autoplay_armed: boolean;
	context_state: string;
	playing: boolean;
	position_ms: number;
	duration_ms: number | null;
	processor_error: string | null;
	autoplay_handoff_in_flight: boolean;
}): UnexpectedPauseCause | null {
	if (input.origin === 'command' || input.origin === 'unload' || input.origin === 'dropout') {
		return null;
	}
	if (input.origin === 'worklet') return null;
	if (input.origin === 'natural-end') return null;
	if (
		input.duration_ms !== null &&
		input.position_ms + EARLY_END_SLACK_MS >= input.duration_ms
	) {
		return null;
	}
	if (input.playing && NON_RUNNING_CONTEXT.has(input.context_state)) {
		return 'context-suspended';
	}
	if (input.processor_error !== null) return 'worklet-error';
	if (
		input.autoplay_handoff_in_flight &&
		input.duration_ms !== null &&
		input.position_ms + EARLY_END_SLACK_MS < input.duration_ms
	) {
		return 'autoplay-handoff-failed';
	}
	if (input.duration_ms !== null && input.position_ms + EARLY_END_SLACK_MS < input.duration_ms) {
		return 'source-ended-early';
	}
	return null;
}

export function formatUnexpectedPauseMessage(input: {
	cause: UnexpectedPauseCause;
	deck: 1 | 2 | 3 | 4;
	position_ms: number;
	context_state?: string;
}): string {
	const parts = [
		`cause=${input.cause}`,
		`deck=${input.deck}`,
		`position_ms=${Math.round(input.position_ms)}`
	];
	if (input.context_state !== undefined) {
		parts.push(`state=${input.context_state}`);
	}
	return `Unexpected pause: ${parts.join(' ')}`;
}
