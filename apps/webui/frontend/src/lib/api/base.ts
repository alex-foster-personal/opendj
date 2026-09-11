/** Same-origin by default; `VITE_API_BASE` points the UI at a remote daemon.
 * THE base URL for the frontend: `src/lib/api.ts` used to resolve a second,
 * identical copy and now re-exports this one, so there is nothing left to
 * drift. Resolved here rather than imported from there because this module is
 * the root of the dependency graph -- importing `$lib/api` would be a cycle. */
const ENV_BASE = import.meta.env.VITE_API_BASE as string | undefined;
export const API_BASE = ENV_BASE ?? '';
