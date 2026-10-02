/**
 * The setup assistant's transport, one-to-one with apps/engine_core/assistant/api.py.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 every request goes to a path this module names once, so the sidebar
 *     never carries a URL literal and an agent driving the same two endpoints
 *     with curl is driving the same contract.
 *     [if] a path string appears in AssistantSidebar.svelte [then ⛔️] broken
 *   ✔︎ 🎯 a refusal keeps the server's code AND message. The daemon already
 *     wrote a sentence a human can act on ("Insufficient credits"); replacing
 *     it with "the assistant is unavailable" hides the only actionable part.
 *     [if] a 409 or 502 is reworded into generic prose [then ⛔️] broken
 *   ✔︎ 🎯 streamChat yields text as it arrives and stops when the signal
 *     aborts, so closing the sidebar mid-reply does not leave a fetch running
 *     against a component that is gone.
 *     [if] an unmounted sidebar keeps reading the body [then ⛔️] broken
 *
 * WHY RAW `fetch` HERE, against the house rule that everything goes through
 * the generated `openapi-fetch` client: /chat answers with a streamed
 * `text/plain` body. openapi-fetch resolves a whole parsed body before it
 * returns, which is exactly the thing this endpoint exists not to do -- going
 * through it would buffer the reply and delete the streaming. `/status` is a
 * plain JSON GET and could use the typed client, but sending the sidebar's
 * two calls down two different transports to save four lines would be worse
 * than the exemption. API_BASE is still imported rather than re-derived, so
 * the remote-daemon setting cannot drift between here and everywhere else.
 */

import { API_BASE } from '$lib/api/base';

/** The two endpoints, spelled once. */
export const ASSISTANT_STATUS_PATH = '/api/v1/assistant/status';
export const ASSISTANT_CHAT_PATH = '/api/v1/assistant/chat';

/** The env var the engine reads its key from. Named in the UI verbatim so a
 * user can act on the message without hunting through docs. Mirrors
 * OPENROUTER_KEY_ENV in apps/engine_core/assistant/api.py. */
export const KEY_ENV_VAR = 'OPENROUTER_API_KEY';

/** Mirrors the refusal codes in apps/engine_core/assistant/api.py. A code
 * outside this list is contract drift, and the sidebar shows the server's
 * own message rather than a branch it does not have. */
export const ASSISTANT_CODES = ['assistant_key_missing', 'assistant_upstream_error'] as const;

export type AssistantCode = (typeof ASSISTANT_CODES)[number];

export interface AssistantStatus {
	/** False when the engine has no API key. There is no partial state. */
	configured: boolean;
	/** The OpenRouter slug that would answer, e.g. `google/gemini-3.7-flash`. */
	model: string;
}

/** A turn. `system` is deliberately absent: the engine supplies the system
 * prompt so that curl and this sidebar get the same assistant. */
export interface ChatMessage {
	role: 'user' | 'assistant';
	content: string;
}

/** A refusal from the daemon, with its code intact. */
export class AssistantError extends Error {
	constructor(
		public readonly code: string,
		message: string,
		/** OpenRouter's own status on a 502, else null. Lets a caller tell
		 * 402-out-of-credit from 429-rate-limited without parsing prose. */
		public readonly upstreamStatus: number | null = null
	) {
		super(message);
		this.name = 'AssistantError';
	}
}

/** Decode `{"detail": {code, message, upstream_status}}` off a failed response. */
async function _refusal(response: Response): Promise<AssistantError> {
	let detail: unknown = null;
	try {
		detail = ((await response.json()) as { detail?: unknown }).detail;
	} catch {
		// A non-JSON error body is itself the information; fall through to the
		// generic branch below rather than hiding that it was not our envelope.
	}
	if (detail && typeof detail === 'object' && 'code' in detail) {
		const typed = detail as { code: string; message?: string; upstream_status?: number };
		return new AssistantError(
			typed.code,
			typed.message ?? typed.code,
			typed.upstream_status ?? null
		);
	}
	return new AssistantError(
		'assistant_unexpected_response',
		`the assistant endpoint answered ${response.status} without an error envelope`
	);
}

/** Whether a chat can work at all, and which model would answer it.
 *
 * Asked before the input box is rendered, so an unconfigured engine shows a
 * notice rather than a text field that fails on the first Enter.
 */
export async function getAssistantStatus(signal?: AbortSignal): Promise<AssistantStatus> {
	const response = await fetch(`${API_BASE}${ASSISTANT_STATUS_PATH}`, {
		signal: signal ?? null
	});
	if (!response.ok) throw await _refusal(response);
	return (await response.json()) as AssistantStatus;
}

/**
 * One completion, yielded in the chunks the daemon streams.
 *
 * The whole conversation is resent every turn because the engine stores none
 * of it. Aborting `signal` cancels the fetch and ends the iteration.
 */
export async function* streamChat(
	messages: ChatMessage[],
	signal: AbortSignal
): AsyncGenerator<string> {
	const response = await fetch(`${API_BASE}${ASSISTANT_CHAT_PATH}`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ messages }),
		signal
	});
	if (!response.ok) throw await _refusal(response);
	if (!response.body) {
		throw new AssistantError(
			'assistant_no_stream',
			'the assistant answered 200 with no body to stream'
		);
	}

	const reader = response.body.getReader();
	const decoder = new TextDecoder();
	try {
		for (;;) {
			const { done, value } = await reader.read();
			if (done) break;
			// `stream: true` so a multi-byte character split across two chunks
			// is held until it is whole rather than rendered as a replacement
			// character.
			yield decoder.decode(value, { stream: true });
		}
		const tail = decoder.decode();
		if (tail) yield tail;
	} finally {
		reader.cancel().catch(() => {
			// The body is already gone when the caller aborted; nothing to do,
			// and nothing worth surfacing to the user over.
		});
	}
}

/**
 * What the setup sidebar shows for one status reading (issue #2590).
 *
 * - 'chat': a key is configured, so the conversation is offered.
 * - 'unavailable': the status could not be read; one plain sentence, with
 *   the engine's words only in the agent disclosure.
 * - 'hidden': nothing for the operator. Covers "not configured" (agents
 *   read GET /api/v1/assistant/status instead), "still checking" (so the
 *   panel never flashes up and away) and "not visible".
 */
export type AssistantPanelMode = 'chat' | 'unavailable' | 'hidden';

export function assistantPanelMode(
	visible: boolean,
	status: Pick<AssistantStatus, 'configured'> | null,
	statusError: string | null
): AssistantPanelMode {
	if (!visible) return 'hidden';
	if (statusError !== null) return 'unavailable';
	if (status?.configured === true) return 'chat';
	return 'hidden';
}
