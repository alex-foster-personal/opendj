/**
 * Classic sampler: typed client for `GET` and `PUT /api/v1/sampler/banks` (issue #9994; server:
 * apps/webui/server/routes/sampler.py, bodies: apps/webui/server/sampler_bank_models.py, store:
 * apps/shared/state/sampler_banks.py). Wire types are hand-written, snake_case, as in api-sampler.ts.
 * A non-2xx answer throws SamplerApiError with the status and the server's body (a 422 names the
 * bad mode or gain); nothing is swallowed. Each call takes an optional `fetchFn`.
 */

import { API_BASE } from '$lib/api/base';
import { SamplerApiError, type FetchFn } from './api-sampler';

const BANKS_URL = `${API_BASE}/api/v1/sampler/banks`;

/** sampler_bank_models.BankMode: the #9989 play modes. */
export type BankMode = 'one_shot' | 'loop' | 'gate';

interface BankSlotCommon {
	mode: BankMode;
	/** -24..+6 dB (sampler_banks.GAIN_DB_MIN..GAIN_DB_MAX) */
	gain_db: number;
	title: string;
}

/** A loaded slot as PUT takes it: a library track or a file on disk, never both. */
export type BankSlot =
	| (BankSlotCommon & { stable_id: string; path?: never })
	| (BankSlotCommon & { path: string; stable_id?: never });

/** A loaded slot as GET answers it: `missing` is true when its stable_id no longer resolves. */
export type BankSlotView = BankSlot & { missing: boolean };

/** BanksPut: the whole document, 4 banks of 16 slots (null = empty). */
export interface BanksDoc {
	v: 1;
	active_bank: number;
	banks: Array<Array<BankSlot | null>>;
}

/** BanksView: GET and PUT answer the document with each loaded slot marked missing or not. */
export interface BanksView {
	v: 1;
	active_bank: number;
	banks: Array<Array<BankSlotView | null>>;
}

async function _banksCall(fetchFn: FetchFn, what: string, init: RequestInit): Promise<BanksView> {
	const headers: Record<string, string> = { Accept: 'application/json' };
	if (init.body !== undefined) headers['Content-Type'] = 'application/json';
	const r = await fetchFn(BANKS_URL, { ...init, headers });
	if (!r.ok) throw new SamplerApiError(r.status, await r.text(), what);
	return (await r.json()) as BanksView;
}

/** GET /banks: the stored banks, 4 x 16 empty when never put. */
export function fetchSamplerBanks(fetchFn: FetchFn = fetch): Promise<BanksView> {
	return _banksCall(fetchFn, 'get banks', { method: 'GET' });
}

/** PUT /banks: replace the whole document atomically, answering it as GET would. */
export function putSamplerBanks(doc: BanksDoc, fetchFn: FetchFn = fetch): Promise<BanksView> {
	return _banksCall(fetchFn, 'save banks', { method: 'PUT', body: JSON.stringify(doc) });
}
