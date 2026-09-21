/**
 * THE typed HTTP client for the music-dj-tools daemon.
 *
 * One `openapi-fetch` instance over the generated `paths` in
 * `src/lib/api-types.ts` (regenerate with `pnpm api:gen`). Every hand-rolled
 * `fetch()` wrapper in the frontend converts onto this instance, so the URL,
 * the request shape and the response type all come from the server's OpenAPI
 * document instead of a hand-copied string literal.
 *
 * Two deliberate design points:
 *
 * 1. ALWAYS THROWS. `openapi-fetch` returns `{ data, error, response }` by
 *    default; the `_throwApiError` middleware below turns every non-2xx into a
 *    thrown `ApiError`, which is the convention the frontend already has
 *    (`_throwRbApiError` in `src/lib/rb/api-rb.ts`, `SmartlistApiError` in
 *    `src/lib/api.ts`). Converted modules therefore keep reading as
 *    `const { data } = await api.GET(...)` with no `if (error)` branch, and a
 *    forgotten error check can never surface as `undefined` data.
 * 2. The error body contract is the backend's `{"detail": {code, message}}`
 *    envelope. `ApiError` also carries `response` and the parsed `body`, so a
 *    call site can reach a header (the ETag on a 409 CAS conflict) or a
 *    conflict payload without re-reading an already-consumed stream.
 *
 * See `src/lib/api/CONVERSION-PATTERN.md` for the module-by-module recipe.
 */

import createFetchClient, { type Middleware } from 'openapi-fetch';

import type { paths } from '../api-types';

import { API_BASE } from './base';

export { API_BASE } from './base';

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

async function _apiErrorFrom(response: Response): Promise<ApiError> {
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

const _throwApiError: Middleware = {
	async onResponse({ response }) {
		if (response.ok) return undefined;
		throw await _apiErrorFrom(response);
	}
};

/** Narrow one client call to its success body.
 *
 * `openapi-fetch` types `data` as possibly-undefined because it models the
 * error branch in the return value; the middleware above has already thrown by
 * the time this runs, so the only way to reach an undefined body is a 204 or a
 * `Content-Length: 0` response. That is a contract violation for the JSON
 * routes this client serves, so it fails loudly rather than returning
 * undefined into UI state. Endpoints that legitimately answer 204 (DELETE)
 * must read `{ response }` directly instead of calling this.
 *
 *     const broken = await unwrap(api.GET('/api/v1/reconcile/broken', {}));
 */
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

export async function unwrap<T>(
	call: Promise<{ data?: T; response: Response }>
): Promise<NonNullable<T>> {
	return requireBody(await call).data;
}

/** The same check as `unwrap`, applied to an already-awaited call, for the
 * call sites that need the RESPONSE as well as the body.
 *
 * Reading an ETag or `x-bind-warning` used to mean destructuring
 * `{ data, response }` straight off the client and then asserting the body
 * onto its real type, because `data` is typed as possibly-undefined. This is
 * that assertion replaced by the check it was standing in for. It is
 * synchronous on purpose: a module that maps HTTP failures onto its own error
 * class awaits the call inside its `try` and calls this AFTER, so a
 * contract-violating empty body is never re-labelled as one of those failures.
 *
 *     const { data, response } = requireBody(await api.GET('/api/v1/health'));
 */
export function requireBody<T>(result: {
	data?: T;
	response: Response;
}): { data: NonNullable<T>; response: Response } {
	const { data, response } = result;
	if (data === undefined || data === null) {
		throw new ApiError(
			response.status,
			'EMPTY_BODY',
			`${response.url} answered ${response.status} with no body`,
			response
		);
	}
	return { data: data as NonNullable<T>, response };
}

/** The one client instance. Import this; never call `createClient` again.
 *
 * `fetch` is forwarded through a closure on purpose: `openapi-fetch` otherwise
 * captures `globalThis.fetch` once, at module init, and every unit test in
 * `tests/unit` intercepts the network by assigning `globalThis.fetch` AFTER
 * importing the module under test. Late binding keeps that idiom working. */
export const api = createFetchClient<paths>({
	baseUrl: API_BASE,
	fetch: (...args) => globalThis.fetch(...args)
});
api.use(_throwApiError);
