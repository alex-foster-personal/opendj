/**
 * CMDK-20..22 (issue #6093): the Cmd K bar's AI search answer. One request at a
 * time per bar; only the latest publishes, so a slow answer for earlier words
 * never lands over newer ones. A failure is shown as a failure, never as "no
 * matches", and no key is its own state so the bar can say how to add one.
 */
import { ApiError, api, unwrap } from '$lib/api';
import type { components } from '$lib/api-types';
import { toastCmdkAi } from './cmdk-ai-toast';

/**
 * The engine calls live here, not in `$lib/api` itself, so they load with the bar's lazy chunk
 * instead of the "/" first-paint closure.
 */

/** CMDK-20 (issue #6093): one shortlisted track as the Cmd K AI search sends it, text only. */
export type CmdkAiTrack = components['schemas']['CmdkAiTrackIn'];

/** Cmd K AI search: whether the engine has an OpenRouter key, without making a call. */
export async function cmdkAiStatus(): Promise<{ configured: boolean; model: string }> {
	return unwrap(api.GET('/api/v1/cmdk/ai-status'));
}

/**
 * Cmd K AI search: P(fits `words`) per shortlisted stable_id, from ONE JEV call
 * on the engine. Resolves `{ok: false, keyMissing: true}` for the engine's 409
 * so the bar can show its add-key row; any other failure rejects.
 */
export async function cmdkAiSearch(
	words: string,
	tracks: CmdkAiTrack[]
): Promise<{ ok: true; scores: Record<string, number> } | { ok: false; keyMissing: true; message: string }> {
	try {
		const out = await unwrap(api.POST('/api/v1/cmdk/ai-search', { body: { words, tracks } }));
		return { ok: true, scores: out.scores };
	} catch (error) {
		if (error instanceof ApiError && error.code === 'ai_search_key_missing') {
			return { ok: false, keyMissing: true, message: error.message };
		}
		throw error;
	}
}

const NO_KEY = 'AI search needs an OpenRouter key.';

export type AiState =
	| { status: 'idle' }
	| { status: 'working'; words: string }
	| { status: 'ready'; words: string; scores: Record<string, number> }
	| { status: 'no-key'; message: string }
	| { status: 'error'; words: string; message: string };

let _state = $state<AiState>({ status: 'idle' });
/**
 * Whether the engine has confirmed a key. `idle` does not say so: it is the state before the check answers and after a
 * reset, so a search must wait for this flag, not for the absence of `no-key` (Codex P2, #6692).
 */
let _keyKnown = $state(false);
let _request = 0;
/** The words and every submitted track field of the last call, so the same question is never paid for twice in a row. */
let _asked = '';

/** No key: the red toast once per state change, and the bar's add-key row. */
function noKey(): void {
	if (_state.status !== 'no-key') toastCmdkAi(NO_KEY);
	_keyKnown = false;
	_state = { status: 'no-key', message: NO_KEY };
}

/** Pure read of the latest answer. */
export function cmdkAi(): AiState {
	return _state;
}

/** Pure read: true only once the engine has confirmed a key for this AI mode visit. */
export function cmdkAiKeyKnown(): boolean {
	return _keyKnown;
}

/** Forget the answer (the bar closed, or left AI mode). Any request in flight no longer publishes. */
export function resetCmdkAi(): void {
	_request++;
	_keyKnown = false;
	_state = { status: 'idle' };
}

/**
 * Before any words are sent: ask the engine whether it has a key, so Tab shows
 * the add-key row at once instead of after a failed search. Mutates rune state:
 * call from an event, never a `$derived`.
 */
export function checkCmdkAiKey(): void {
	const request = ++_request;
	void cmdkAiStatus().then(
		(status) => {
			if (request !== _request) return;
			if (status.configured) {
				_keyKnown = true;
				_state = { status: 'idle' };
			}
			else noKey();
		},
		(error: unknown) => {
			if (request === _request) _state = { status: 'error', words: '', message: error instanceof Error ? error.message : String(error) };
		}
	);
}

/**
 * Whether the last call asked exactly these words about exactly these track fields: a `ready`
 * answer scores the rows shown only then. A refresh that edits a track under the same id and
 * query hides the old scores until the new call answers, never ranking the new rows by the old
 * metadata during the debounce (Codex r4277881958).
 */
export function cmdkAiAnswers(words: string, tracks: CmdkAiTrack[]): boolean {
	return _asked === JSON.stringify([words, tracks]);
}

/** Rank `tracks` against `words` with one engine call. Mutates rune state: call from an event or a timer. */
export function requestCmdkAi(words: string, tracks: CmdkAiTrack[]): void {
	// The shortlist re-derives when the master's tempo fader moves; the same tracks for the
	// same words are already answered (or being answered), so that asks nothing. The key is
	// everything sent, not only the ids: a refresh that edits a track's title, genre, BPM or
	// key asks again rather than reviving a ranking of the old metadata (Codex r4276714952).
	const asked = JSON.stringify([words, tracks]);
	if (asked === _asked && (_state.status === 'working' || _state.status === 'ready') && _state.words === words) return;
	_asked = asked;
	const request = ++_request;
	if (tracks.length === 0) {
		_state = { status: 'ready', words, scores: {} };
		return;
	}
	_state = { status: 'working', words };
	void cmdkAiSearch(words, tracks).then(
		(out) => {
			if (request !== _request) return;
			if (out.ok) _state = { status: 'ready', words, scores: out.scores };
			else noKey();
		},
		(error: unknown) => {
			if (request === _request) _state = { status: 'error', words, message: error instanceof Error ? error.message : String(error) };
		}
	);
}
