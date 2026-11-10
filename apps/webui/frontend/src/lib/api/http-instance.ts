/**
 * The one typed `openapi-fetch` client for the music-dj-tools daemon.
 *
 * `openapi-fetch` returns `{ data, error, response }` by default; the
 * `_throwApiError` middleware below turns every non-2xx into a thrown
 * `ApiError`, which is the convention the frontend already has. Converted
 * modules therefore keep reading as `const { data } = await api.GET(...)` with
 * no `if (error)` branch.
 */

import createFetchClient, { type Middleware } from 'openapi-fetch';

import type { paths } from '../api-types';

import { API_BASE } from './base';
import { apiErrorFrom } from './errors';

const _throwApiError: Middleware = {
	async onResponse({ response }) {
		if (response.ok) return undefined;
		throw await apiErrorFrom(response);
	}
};

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
