/**
 * Load and save outcomes for the small standalone routes (/queues, /pairings,
 * /track/[stable_id]).
 *
 * Those pages used to `await` their fetch inside `onMount` with no catch, so a
 * failed request left them blank (queues, pairings) or on "Loading..." forever
 * (an unknown track id). These pure helpers decide what the page says instead,
 * so the wording and the not-found rule are unit tested without a component.
 */
import { readApiErrorStatus } from './api/client';

/** What a route shows after its load settles. */
export type RouteLoadError = {
	/** 'not-found' when the daemon answered 404, else 'error'. */
	kind: 'not-found' | 'error';
	/** The reason, readable on its own: never empty. */
	message: string;
};

/** The failure's own words, or a stand-in that says the reason was empty. */
export function describeLoadError(exc: unknown): string {
	let text = '';
	if (exc instanceof Error) text = exc.message;
	else if (typeof exc === 'string') text = exc;
	else if (exc !== null && exc !== undefined) text = String(exc);
	text = text.trim();
	return text === '' ? 'request failed with no error message' : text;
}

/** Classify a failed load: a 404 is "not found", anything else an error. */
export function routeLoadError(exc: unknown): RouteLoadError {
	return {
		kind: readApiErrorStatus(exc) === 404 ? 'not-found' : 'error',
		message: describeLoadError(exc)
	};
}

/**
 * Whether a notes field edit needs saving. A blur with the same text as the
 * saved value is not an edit: saving it anyway costs a PATCH, bumps the ETag
 * and toasts "Saved" for nothing. A missing saved value equals empty text,
 * matching how the textarea renders `track.notes ?? ''`.
 */
export function notesChanged(saved: string | null | undefined, next: string): boolean {
	return (saved ?? '') !== next;
}
