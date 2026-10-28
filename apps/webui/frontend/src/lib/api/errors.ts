/** Shared daemon error contracts, independent of the HTTP client instance.
 * Supersedes: error definitions in api/client.ts and rb/api-rb-error.ts;
 * those public modules re-export these same constructors for compatibility.
 */

/** A non-2xx response from the daemon, decoded from `{"detail": {code, message}}`. */
export class ApiError extends Error {
	constructor(
		public readonly status: number,
		public readonly code: string,
		message: string,
		/** The failing response with headers intact (ETag on a 409, x-bind-warning, ...). */
		public readonly response: Response,
		/** The parsed error body, or null when the body was empty or not JSON. */
		public readonly body: unknown = null,
		/** Full raw response text read when the error was constructed; empty when none. */
		public readonly bodyText: string = ''
	) {
		super(message);
		this.name = 'ApiError';
	}
}

/** Backend error envelope. `detail` as an object is our routers' convention
 * (`HTTPException(detail={"code": ..., "message": ...})`); FastAPI's own
 * validation layer raises a string or a list instead. */
interface ErrorEnvelope {
	detail?: { code?: string; message?: string } | string | unknown[];
}

/** Consume the original response body once and return raw text plus parsed JSON.
 * Empty text yields `body: null` and `bodyText: ''`; invalid JSON keeps the raw
 * text while `body` is null. Bodyless documented errors (dedup 409, relocate 428)
 * are the one place this diverges from `_throwRbApiError`, which assumes JSON. */
async function _decodeErrorBody(response: Response): Promise<{ body: unknown; bodyText: string }> {
	const bodyText = await response.text();
	if (bodyText === '') return { body: null, bodyText: '' };
	try {
		return { body: JSON.parse(bodyText) as unknown, bodyText };
	} catch {
		return { body: null, bodyText };
	}
}

/** Decode one non-2xx response into the ApiError this client throws. Exported
 * for the one raw-fetch caller whose route is not in the generated schema yet
 * (`getTrack` for a stick id, spec 4b), so its failures carry the same
 * `code` a typed call would. */
export async function apiErrorFrom(response: Response): Promise<ApiError> {
	const { body, bodyText } = await _decodeErrorBody(response);
	const detail = (body as ErrorEnvelope | null)?.detail;
	const structured =
		typeof detail === 'object' && detail !== null && !Array.isArray(detail) ? detail : undefined;
	const code = structured?.code ?? `HTTP_${response.status}`;
	const message =
		structured?.message ??
		(typeof detail === 'string' ? detail : undefined) ??
		(response.statusText === '' ? `HTTP ${response.status}` : response.statusText);
	return new ApiError(response.status, code, message, response, body, bodyText);
}

/** Read an ApiError's HTTP status without relying on `instanceof` alone.
 * Unit tests bundle modules in isolation, which can produce a second ApiError
 * class identity even though the thrown value is otherwise identical. */
export function readApiErrorStatus(error: unknown): number | null {
	if (error instanceof ApiError) return error.status;
	if (
		typeof error === 'object' &&
		error !== null &&
		(error as ApiError).name === 'ApiError' &&
		typeof (error as ApiError).status === 'number'
	) {
		return (error as ApiError).status;
	}
	return null;
}

/** Read an ApiError's detail.code without relying on `instanceof` alone. */
export function readApiErrorCode(error: unknown): string | null {
	if (error instanceof ApiError) return error.code;
	if (
		typeof error === 'object' &&
		error !== null &&
		(error as ApiError).name === 'ApiError' &&
		typeof (error as ApiError).code === 'string'
	) {
		return (error as ApiError).code;
	}
	if (typeof error === 'object' && error !== null) {
		const body = (error as { body?: ErrorEnvelope | null }).body;
		const detail = body?.detail;
		if (typeof detail === 'object' && detail !== null && !Array.isArray(detail)) {
			const code = detail.code;
			if (typeof code === 'string') return code;
		}
	}
	return null;
}

/** Explicit backend error: HTTP status + the contract's detail.code. */
export class RbApiError extends Error {
	constructor(
		public status: number,
		public code: string,
		message: string,
		/** The raw `{detail: {...}}` error body, when a caller chose to keep
		 * it - most callers only need code/message, so this defaults to null
		 * rather than forcing every construction site to thread it through.
		 * beatgrid-upgrade.ts reads `body.detail.anlz_available` off a 404
		 * here: the same field a 200 response carries, but otherwise lost the
		 * moment ApiError converts onto this type. */
		public body: unknown = null
	) {
		super(`${code}: ${message}`);
		this.name = 'RbApiError';
	}
}


/** What fetch rejects with when nothing answered. The text is the runtime's
 * own and differs per engine (Chromium, WebKit, Gecko, Node). */
const UNREACHABLE_FETCH_MESSAGES: ReadonlySet<string> = new Set([
	'Failed to fetch',
	'Load failed',
	'NetworkError when attempting to fetch resource.',
	'fetch failed'
]);

/** Wording only: does this read like fetch's no-answer rejection? For a
 * headline over a failure that is shown anyway (deck load, whose cause can
 * come from a raw `fetch` outside the API client). Text alone is not
 * provenance, so never use it to decide that a failure stays quiet. */
export function readsAsUnreachableFetch(cause: unknown): boolean {
	return cause instanceof TypeError && UNREACHABLE_FETCH_MESSAGES.has(cause.message);
}
