/**
 * GET-JSON entry for route-lazy modules. Importing this instead of api-rb
 * keeps those modules off api-rb's fan-in; the fetch implementation stays there.
 */
export { fetchRbJson, RbApiError } from './api-rb';
