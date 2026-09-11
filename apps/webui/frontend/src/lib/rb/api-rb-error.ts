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
