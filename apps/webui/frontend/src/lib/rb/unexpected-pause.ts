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

export type UnexpectedPauseFormatInput = {
	cause: UnexpectedPauseCause;
	deck: 1 | 2 | 3 | 4;
	position_ms: number;
	context_state?: string;
	decoded_duration_ms?: number | null;
	metadata_duration_ms?: number | null;
};

export function formatMsAsClock(duration_ms: number): string {
	const totalSeconds = Math.floor(duration_ms / 1000);
	const minutes = Math.floor(totalSeconds / 60);
	const seconds = totalSeconds % 60;
	return `${minutes}:${seconds.toString().padStart(2, '0')}`;
}

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

export function formatUnexpectedPauseDiagnostic(input: UnexpectedPauseFormatInput): string {
	const parts = [
		`cause=${input.cause}`,
		`deck=${input.deck}`,
		`position_ms=${Math.round(input.position_ms)}`
	];
	if (input.context_state !== undefined) {
		parts.push(`state=${input.context_state}`);
	}
	if (input.cause === 'source-ended-early') {
		const decoded = input.decoded_duration_ms ?? null;
		const metadata = input.metadata_duration_ms ?? null;
		parts.push(`decoded_duration_ms=${decoded === null ? 'null' : Math.round(decoded)}`);
		parts.push(`metadata_duration_ms=${metadata === null ? 'null' : Math.round(metadata)}`);
	}
	return `Unexpected pause: ${parts.join(' ')}`;
}

export function formatUnexpectedPauseMessage(input: UnexpectedPauseFormatInput): string {
	const deckLabel = `Deck ${input.deck}`;
	const stopTime = formatMsAsClock(input.position_ms);
	switch (input.cause) {
		case 'source-ended-early': {
			const metadataMs = input.metadata_duration_ms ?? null;
			if (metadataMs !== null) {
				return `${deckLabel} stopped at ${stopTime}, before the track's listed end (${formatMsAsClock(metadataMs)}). The audio file may be cut short.`;
			}
			const decodedMs = input.decoded_duration_ms ?? null;
			if (decodedMs !== null) {
				return `${deckLabel} stopped at ${stopTime}, before the decoded audio ends (${formatMsAsClock(decodedMs)}). The audio file may be cut short.`;
			}
			return `${deckLabel} stopped at ${stopTime} before the track should have ended. The listed length is unavailable. The audio file may be cut short.`;
		}
		case 'context-suspended':
			return `${deckLabel} stopped unexpectedly. Audio output was interrupted.`;
		case 'worklet-error':
			return `${deckLabel} stopped unexpectedly. The audio processor failed.`;
		case 'autoplay-handoff-failed':
			return `${deckLabel} stopped unexpectedly while AutoPlay was switching tracks.`;
		default: {
			const _exhaustive: never = input.cause;
			throw new Error(`Unhandled unexpected-pause cause: ${_exhaustive}`);
		}
	}
}
