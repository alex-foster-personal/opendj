import { ApiError } from './errors';

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
export function requireBody<T>(result: { data?: T; response: Response }): {
	data: NonNullable<T>;
	response: Response;
} {
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
