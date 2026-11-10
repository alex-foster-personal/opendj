/** Admin-route HTTP entry for later consumer retargeting off `client.ts`. */
export { api } from './http-instance';
export { unwrap, requireBody } from './unwrap';
export { API_BASE } from './base';
export { ApiError, RbApiError, apiErrorFrom, readApiErrorStatus, readApiErrorCode } from './errors';
