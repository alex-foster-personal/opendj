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
 * 1. ALWAYS THROWS. Non-2xx responses become thrown `ApiError` values in
 *    `http-instance.ts`; see that module and `src/lib/api/errors.ts`.
 * 2. The error body contract is the backend's `{"detail": {code, message}}`
 *    envelope. Non-2xx responses are consumed once in `apiErrorFrom`; `ApiError`
 *    retains `bodyText` (full raw payload), `body` (parsed JSON or null), and
 *    `response` (headers only after the body is consumed).
 *
 * Temporary re-export barrel: importers still use this path; domain entry modules
 * (`rb-http.ts`, `admin-http.ts`, `app-http.ts`) are the retarget for follow-up PRs.
 *
 * See `src/lib/api/CONVERSION-PATTERN.md` for the module-by-module recipe.
 */

export { api } from './http-instance';
export { unwrap, requireBody } from './unwrap';
export { API_BASE } from './base';
export { ApiError, RbApiError, apiErrorFrom, readApiErrorStatus, readApiErrorCode } from './errors';
