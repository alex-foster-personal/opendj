/**
 * Framework-neutral HTTP client for comment pins (issue #11295, parent #3784).
 * Uses the same `/api/v1/feedback` paths as `feedback-store.svelte.ts`.
 */
import type { components } from '../../api-types';

export type CommentPin = components['schemas']['CommentOut'];
export type CommentCreateIn = components['schemas']['CommentCreateIn'];

export type CommentPinsApiPaths = {
	comments: string;
	replies: (id: string) => string;
	archive: (id: string) => string;
	attachment: (id: string) => string;
	patch: (id: string) => string;
};

const DEFAULT_PATHS: CommentPinsApiPaths = {
	comments: '/api/v1/feedback/comments',
	replies: (id) => `/api/v1/feedback/comments/${encodeURIComponent(id)}/replies`,
	archive: (id) => `/api/v1/feedback/comments/${encodeURIComponent(id)}/archive`,
	attachment: (id) => `/api/v1/feedback/comments/${encodeURIComponent(id)}/attachment`,
	patch: (id) => `/api/v1/feedback/comments/${encodeURIComponent(id)}`
};

export interface CommentPinsApiOptions {
	apiBase?: string;
	paths?: CommentPinsApiPaths;
	fetchFn?: typeof fetch;
}

async function _readJson<T>(response: Response): Promise<T> {
	if (!response.ok) {
		let detail = `HTTP ${response.status}`;
		try {
			const body = (await response.json()) as { detail?: { message?: string } | string };
			if (typeof body.detail === 'string') detail = body.detail;
			else if (typeof body.detail?.message === 'string') detail = body.detail.message;
		} catch {
			// keep generic detail
		}
		throw new Error(detail);
	}
	return (await response.json()) as T;
}

export class CommentPinsApi {
	readonly #base: string;
	readonly #paths: CommentPinsApiPaths;
	readonly #fetch: typeof fetch;

	constructor(options: CommentPinsApiOptions = {}) {
		this.#base = options.apiBase ?? '';
		this.#paths = options.paths ?? DEFAULT_PATHS;
		// Forward through globalThis: native fetch called as this.#fetch(...) throws Illegal invocation.
		this.#fetch = options.fetchFn ?? ((input, init) => globalThis.fetch(input, init));
	}

	url(path: string): string {
		return `${this.#base}${path}`;
	}

	async listComments(): Promise<CommentPin[]> {
		const body = await _readJson<{ comments: CommentPin[] }>(
			await this.#fetch(this.url(this.#paths.comments))
		);
		return body.comments;
	}

	async createComment(body: CommentCreateIn): Promise<CommentPin> {
		return _readJson<CommentPin>(
			await this.#fetch(this.url(this.#paths.comments), {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(body)
			})
		);
	}

	async addReply(pinId: string, text: string): Promise<CommentPin> {
		return _readJson<CommentPin>(
			await this.#fetch(this.url(this.#paths.replies(pinId)), {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ text, author: 'operator' })
			})
		);
	}

	/** Operator resolve: archive removes the pin from the live board. */
	async archiveComment(pinId: string): Promise<void> {
		await _readJson<unknown>(
			await this.#fetch(this.url(this.#paths.archive(pinId)), { method: 'POST' })
		);
	}

	/** Agent/operator lifecycle resolve (fixed or merged). */
	async patchComment(
		pinId: string,
		patch: { status?: 'fixed' | 'merged'; issue_url?: string | null; agent_note?: string | null }
	): Promise<CommentPin> {
		return _readJson<CommentPin>(
			await this.#fetch(this.url(this.#paths.patch(pinId)), {
				method: 'PATCH',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(patch)
			})
		);
	}

	async uploadAttachment(pinId: string, file: File): Promise<CommentPin> {
		const form = new FormData();
		form.append('file', file, file.name);
		return _readJson<CommentPin>(
			await this.#fetch(this.url(this.#paths.attachment(pinId)), { method: 'POST', body: form })
		);
	}
}

export function commentPathsForPrefix(prefix: string): CommentPinsApiPaths {
	const base = prefix.endsWith('/') ? prefix.slice(0, -1) : prefix;
	return {
		comments: `${base}/comments`,
		replies: (id) => `${base}/comments/${encodeURIComponent(id)}/replies`,
		archive: (id) => `${base}/comments/${encodeURIComponent(id)}/archive`,
		attachment: (id) => `${base}/comments/${encodeURIComponent(id)}/attachment`,
		patch: (id) => `${base}/comments/${encodeURIComponent(id)}`
	};
}
