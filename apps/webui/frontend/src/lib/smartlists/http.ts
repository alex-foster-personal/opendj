/**
 * Typed smartlist HTTP client used by /smartlists (list, create, detail, save).
 *
 * Lives next to the rule-form AST helpers so api.ts stays under the frontend
 * file-size ratchet. The browser tree keeps using `$lib/rb/api-smartlists.ts`.
 */
import type { components } from '../api-types';
import { ApiError, api, requireBody, unwrap } from '../api/client';
import type { RuleAst } from './rule-form';

/** `rule` is an open record here, not a `RuleAst`: the daemon documents it as
 * one (`SmartlistSummary.rule`) and the editor narrows it with `astToForm`. */
export type SmartlistOut = components['schemas']['SmartlistSummary'];

export class SmartlistApiError extends Error {
	constructor(public status: number, message: string) {
		super(message);
	}
}

export class SmartlistConflictError extends Error {
	constructor(public current: SmartlistOut, public etag: string) {
		super('Smartlist If-Match mismatch');
	}
}

export type SmartlistTrackOut = components['schemas']['TrackRowOut'];

/** List every stored smartlist. Create lives on POST /api/v1/smartlists. */
export async function listSmartlists(): Promise<SmartlistOut[]> {
	try {
		return await unwrap(api.GET('/api/v1/smartlists'));
	} catch (error) {
		if (error instanceof ApiError) throw new Error(`GET smartlists failed: ${error.status}`);
		throw error;
	}
}

/** Create a smartlist through the same HTTP path the editor then saves with.
 * The returned object is server-persisted readback, never an optimistic copy. */
export async function createSmartlist(
	body: { name: string; rule: RuleAst; order_by?: string }
): Promise<{ smartlist: SmartlistOut; etag: string }> {
	let call: { data?: SmartlistOut; response: Response };
	try {
		call = await api.POST('/api/v1/smartlists', {
			body: {
				name: body.name,
				rule: { ...body.rule },
				order_by: body.order_by ?? null
			}
		});
	} catch (error) {
		if (error instanceof ApiError) {
			throw new SmartlistApiError(error.status, `POST smartlist failed: ${error.status}`);
		}
		throw error;
	}
	const { data, response } = requireBody(call);
	const etag = requiredSmartlistEtag(response);
	return { smartlist: data, etag };
}

function requiredSmartlistEtag(response: Response): string {
	const etag = response.headers.get('etag');
	if (etag === null || etag.length === 0) {
		throw new Error('smartlist response is missing ETag');
	}
	return etag;
}

export async function getSmartlist(
	id: string
): Promise<{ smartlist: SmartlistOut; etag: string }> {
	let call: { data?: SmartlistOut; response: Response };
	try {
		call = await api.GET('/api/v1/smartlists/{smartlist_id}', {
			params: { path: { smartlist_id: id } }
		});
	} catch (error) {
		if (error instanceof ApiError) {
			throw new SmartlistApiError(error.status, `GET smartlist failed: ${error.status}`);
		}
		throw error;
	}
	const { data, response } = requireBody(call);
	const etag = requiredSmartlistEtag(response);
	return { smartlist: data, etag };
}

export async function getSmartlistTracks(id: string): Promise<SmartlistTrackOut[]> {
	try {
		const body = await unwrap(
			api.GET('/api/v1/smartlists/{smartlist_id}/tracks', {
				params: { path: { smartlist_id: id } }
			})
		);
		return body.tracks;
	} catch (error) {
		if (error instanceof ApiError) throw new Error(`GET smartlist tracks failed: ${error.status}`);
		throw error;
	}
}

/** Replace a smartlist rule through the same HTTP apply path as the editor.
 * The returned object is server-persisted readback, never an optimistic copy. */
export async function updateSmartlist(
	id: string,
	body: { rule: RuleAst; order_by?: string },
	etag: string
): Promise<{ smartlist: SmartlistOut; etag: string }> {
	let call: { data?: SmartlistOut; response: Response };
	try {
		call = await api.PUT('/api/v1/smartlists/{smartlist_id}', {
			params: { path: { smartlist_id: id }, header: { 'If-Match': etag } },
			// A spread, not an assertion: `RuleAst` is a closed union of
			// interfaces and the schema's `rule` is an open record, so the AST
			// has to widen into one rather than be asserted onto it. `?? null`
			// for the same exactOptionalPropertyTypes reason as listPairings
			// above: the generated type is `order_by?: string | null`.
			body: { rule: { ...body.rule }, order_by: body.order_by ?? null }
		});
	} catch (error) {
		if (error instanceof ApiError && error.status === 409) {
			// The conflict envelope is top-level {current, etag}; the header and
			// the body must agree before the caller is handed a revision to retry
			// against, so a half-updated response can never drive a silent clobber.
			const responseEtag = requiredSmartlistEtag(error.response);
			const payload = error.body as { current: SmartlistOut; etag: string };
			if (payload.etag !== responseEtag) {
				throw new Error('smartlist conflict response ETag does not match its body');
			}
			throw new SmartlistConflictError(payload.current, responseEtag);
		}
		if (error instanceof ApiError) {
			const payload = (error.body ?? {}) as { detail?: { message?: string } | string };
			const detail = typeof payload.detail === 'object' ? payload.detail?.message : payload.detail;
			throw new SmartlistApiError(error.status, detail ?? `PUT smartlist failed: ${error.status}`);
		}
		throw error;
	}
	const { data, response } = requireBody(call);
	const nextEtag = requiredSmartlistEtag(response);
	return { smartlist: data, etag: nextEtag };
}
