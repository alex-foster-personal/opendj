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

import { ApiError, apiErrorFrom } from './errors';

export { ApiError, RbApiError, apiErrorFrom, readApiErrorStatus, readApiErrorCode } from './errors';

const _throwApiError: Middleware = {
	async onResponse({ response }) {
		if (response.ok) return undefined;
		throw await apiErrorFrom(response);
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
