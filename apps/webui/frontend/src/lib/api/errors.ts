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
		public readonly body: unknown = null
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

/** Read the error body without consuming the caller's copy of the stream.
 * Returns null for the bodyless error responses the API documents (dedup 409,
 * relocate 428), which is the one place this diverges from
 * `_throwRbApiError`: that helper assumes every error carries JSON. */
async function _decodeErrorBody(response: Response): Promise<unknown> {
	const raw = await response.clone().text();
	if (raw === '') return null;
	try {
		return JSON.parse(raw) as unknown;
	} catch {
		return null;
	}
}

/** Decode one non-2xx response into the ApiError this client throws. Exported
 * for the one raw-fetch caller whose route is not in the generated schema yet
 * (`getTrack` for a stick id, spec 4b), so its failures carry the same
 * `code` a typed call would. */
export async function apiErrorFrom(response: Response): Promise<ApiError> {
	const body = await _decodeErrorBody(response);
	const detail = (body as ErrorEnvelope | null)?.detail;
	const structured =
		typeof detail === 'object' && detail !== null && !Array.isArray(detail) ? detail : undefined;
	const code = structured?.code ?? `HTTP_${response.status}`;
	const message =
		structured?.message ??
		(typeof detail === 'string' ? detail : undefined) ??
		(response.statusText === '' ? `HTTP ${response.status}` : response.statusText);
	return new ApiError(response.status, code, message, response, body);
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

