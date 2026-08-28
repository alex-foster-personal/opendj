/**
 * Route-local fetch helper for GET /api/v1/bench/kpi.
 *
 * The browser never reads scripts/bench/kpi_ledger.json off disk - the daemon
 * serves it, so an agent can curl exactly what the panel renders.
 *
 * Fail-fast: the response is structurally validated before render. A missing
 * `notes` key is tolerated (an unwritten note is absence, not corruption); a
 * wrong TYPE anywhere is an explicit error rather than a junk tile.
 */

import { ApiError, api, unwrap } from '$lib/api/client';

export interface KpiDef {
	label: string;
	unit: string;
	direction: 'lower_better' | 'higher_better';
	title: string;
}

export interface KpiSnapshot {
	ts: string;
	label: string;
	values: Record<string, number | null>;
	/**
	 * Per-key origin: "derived" (recomputed from per-track telemetry by
	 * scripts/bench/kpi_derive.py), "hand"/"hand-unverifiable" (typed by a
	 * human, no telemetry behind it), "configured-not-measured" (copied from a
	 * config constant), "not-derivable" or "unmeasured". Empty for snapshots
	 * written before provenance existed.
	 */
	provenance: Record<string, string>;
	notes: string | null;
}

export interface KpiLedger {
	kpis: Record<string, KpiDef>;
	snapshots: KpiSnapshot[];
}

//----- validators -----------------------------------------------------------

function asObject(value: unknown, ctx: string): Record<string, unknown> {
	if (typeof value !== 'object' || value === null || Array.isArray(value)) {
		throw new Error(`kpi ledger: ${ctx} is not an object`);
	}
	return value as Record<string, unknown>;
}

function asString(value: unknown, ctx: string): string {
	if (typeof value !== 'string') throw new Error(`kpi ledger: ${ctx} is not a string`);
	return value;
}

function parseKpi(raw: unknown, key: string): KpiDef {
	const obj = asObject(raw, `kpis.${key}`);
	const direction = asString(obj.direction, `kpis.${key}.direction`);
	if (direction !== 'lower_better' && direction !== 'higher_better') {
		throw new Error(
			`kpi ledger: kpis.${key}.direction must be lower_better or higher_better, got '${direction}'`
		);
	}
	return {
		label: asString(obj.label, `kpis.${key}.label`),
		unit: asString(obj.unit, `kpis.${key}.unit`),
		direction,
		title: asString(obj.title, `kpis.${key}.title`)
	};
}

function parseSnapshot(raw: unknown, index: number): KpiSnapshot {
	const obj = asObject(raw, `snapshots[${index}]`);
	const values = asObject(obj.values, `snapshots[${index}].values`);
	const parsed: Record<string, number | null> = {};
	for (const [key, value] of Object.entries(values)) {
		if (value !== null && typeof value !== 'number') {
			throw new Error(`kpi ledger: snapshots[${index}].values.${key} is not a number or null`);
		}
		parsed[key] = value;
	}
	// An absent note means nobody wrote one; only a non-string note is corruption.
	const notes = obj.notes;
	if (notes !== undefined && notes !== null && typeof notes !== 'string') {
		throw new Error(`kpi ledger: snapshots[${index}].notes is not a string or null`);
	}
	// Same tolerance for provenance: absent means a pre-provenance snapshot, but
	// a non-string origin is corruption and must not render as a claim of origin.
	const provenance: Record<string, string> = {};
	if (obj.provenance !== undefined && obj.provenance !== null) {
		const raw = asObject(obj.provenance, `snapshots[${index}].provenance`);
		for (const [key, origin] of Object.entries(raw)) {
			if (typeof origin !== 'string') {
				throw new Error(`kpi ledger: snapshots[${index}].provenance.${key} is not a string`);
			}
			provenance[key] = origin;
		}
	}
	return {
		ts: asString(obj.ts, `snapshots[${index}].ts`),
		label: asString(obj.label, `snapshots[${index}].label`),
		values: parsed,
		provenance,
		notes: notes === undefined ? null : notes
	};
}

function parseLedger(raw: unknown): KpiLedger {
	const obj = asObject(raw, 'response');
	const kpisRaw = asObject(obj.kpis, 'kpis');
	if (!Array.isArray(obj.snapshots)) throw new Error('kpi ledger: snapshots is not an array');
	if (obj.snapshots.length === 0) throw new Error('kpi ledger: snapshots is empty');

	const kpis: Record<string, KpiDef> = {};
	for (const [key, value] of Object.entries(kpisRaw)) kpis[key] = parseKpi(value, key);
	return { kpis, snapshots: obj.snapshots.map(parseSnapshot) };
}

/** TEST-ONLY: the validator half, without a network round trip. */
export const _parseLedgerForTests = parseLedger;

//----- fetch ----------------------------------------------------------------

export async function fetchKpiLedger(): Promise<KpiLedger> {
	try {
		const data = await unwrap(api.GET('/api/v1/bench/kpi', { cache: 'no-store' }));
		return parseLedger(data);
	} catch (error) {
		if (error instanceof ApiError) {
			const bodyText = await error.response.text();
			throw new Error(`GET /api/v1/bench/kpi failed (HTTP ${error.status}): ${bodyText}`);
		}
		throw error;
	}
}
