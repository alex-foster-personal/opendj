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

/** Same-origin by default; `VITE_API_BASE` points the UI at a remote daemon.
 * Same value as `API_BASE` in `src/lib/api.ts`; that copy retires when
 * `api.ts` itself converts. Resolved here rather than imported from there so
 * this module stays the root of the dependency graph (importing `$lib/api`
 * would become a cycle the moment `api.ts` converts onto this client). */
const ENV_BASE = import.meta.env.VITE_API_BASE as string | undefined;
export const API_BASE = ENV_BASE ?? '';

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
export async function unwrap<T>(
	call: Promise<{ data?: T; response: Response }>
): Promise<NonNullable<T>> {
	const { data, response } = await call;
	if (data === undefined || data === null) {
		throw new ApiError(
			response.status,
			'EMPTY_BODY',
			`${response.url} answered ${response.status} with no body`,
			response
		);
	}
	return data as NonNullable<T>;
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
