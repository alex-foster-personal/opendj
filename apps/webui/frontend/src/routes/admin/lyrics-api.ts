/**
 * Route-local fetch helpers for the admin lyrics operations area.
 *
 * Same contract as kpi-api.ts: the browser only ever talks to the daemon
 * (never files on disk), every response is structurally validated before
 * render, and a wrong TYPE anywhere is an explicit error rather than a junk
 * panel. Error responses surface the server's `detail` verbatim - a 422 from
 * PUT /lyrics/config or POST /lyrics/jobs is an operator-facing validation
 * message, not something to paraphrase.
 *
 * Types mirror $lib/api where they exist; only the lyrics KPI ledger needs
 * its own parsing because that ledger stamps snapshot provenance as a single
 * string (lyrics_kpi_append.py) where the demucs ledger uses a per-metric
 * map, and kpi-api's parser rightly rejects the string form.
 */

import {
	API_BASE,
	type LyricJob,
	type LyricsConfig,
	type LyricSummary,
	type LyricVerdict,
	type LyricVerdictValue
} from '$lib/api';
import type { KpiDef, KpiLedger, KpiSnapshot } from './kpi-api';

const VERDICT_VALUES: readonly LyricVerdictValue[] = ['vocal', 'sparse', 'no-lyrics', 'unknown'];
const JOB_KINDS: readonly LyricJob['kind'][] = ['analyze', 'lyricsync', 'stems'];
const JOB_STATUSES: readonly LyricJob['status'][] = ['queued', 'done', 'failed'];

//----- shared validators -----------------------------------------------------

function _asObject(value: unknown, ctx: string): Record<string, unknown> {
	if (typeof value !== 'object' || value === null || Array.isArray(value)) {
		throw new Error(`lyrics api: ${ctx} is not an object`);
	}
	return value as Record<string, unknown>;
}

function _asArray(value: unknown, ctx: string): unknown[] {
	if (!Array.isArray(value)) throw new Error(`lyrics api: ${ctx} is not an array`);
	return value;
}

function _asString(value: unknown, ctx: string): string {
	if (typeof value !== 'string') throw new Error(`lyrics api: ${ctx} is not a string`);
	return value;
}

function _asBoolean(value: unknown, ctx: string): boolean {
	if (typeof value !== 'boolean') throw new Error(`lyrics api: ${ctx} is not a boolean`);
	return value;
}

function _asNumber(value: unknown, ctx: string): number {
	if (typeof value !== 'number') throw new Error(`lyrics api: ${ctx} is not a number`);
	return value;
}

function _asNumberOrNull(value: unknown, ctx: string): number | null {
	if (value === null || value === undefined) return null;
	return _asNumber(value, ctx);
}

function _asStringOrNull(value: unknown, ctx: string): string | null {
	if (value === null || value === undefined) return null;
	return _asString(value, ctx);
}

function _asVerdictValue(value: unknown, ctx: string): LyricVerdictValue {
	const s = _asString(value, ctx);
	if (!VERDICT_VALUES.includes(s as LyricVerdictValue)) {
		throw new Error(`lyrics api: ${ctx} must be one of ${VERDICT_VALUES.join('|')}, got '${s}'`);
	}
	return s as LyricVerdictValue;
}

//----- error surfacing -------------------------------------------------------

/** Throw with the server's `detail` verbatim. The body may be FastAPI JSON
 *  ({detail: ...}) or plain text; the try/catch below only picks the
 *  representation - a failed response ALWAYS throws. */
async function _fail(method: string, path: string, resp: Response): Promise<never> {
	const body = await resp.text();
	let detail = body;
	try {
		const parsed: unknown = JSON.parse(body);
		if (typeof parsed === 'object' && parsed !== null && 'detail' in parsed) {
			const d = (parsed as { detail: unknown }).detail;
			detail = typeof d === 'string' ? d : JSON.stringify(d);
		}
	} catch {
		// body was not JSON; the raw text is already the most faithful detail
	}
	throw new Error(`${method} ${path} failed (HTTP ${resp.status}): ${detail}`);
}

async function _requestJson(
	method: 'GET' | 'PUT' | 'POST',
	path: string,
	body?: unknown
): Promise<unknown> {
	const init: RequestInit = { method, cache: 'no-store' };
	if (body !== undefined) {
		init.headers = { 'Content-Type': 'application/json' };
		init.body = JSON.stringify(body);
	}
	const resp = await fetch(`${API_BASE}${path}`, init);
	if (!resp.ok) await _fail(method, path, resp);
	return resp.json();
}

//----- lyrics KPI ledger -----------------------------------------------------

function _parseKpiDef(raw: unknown, key: string): KpiDef {
	const obj = _asObject(raw, `kpis.${key}`);
	const direction = _asString(obj.direction, `kpis.${key}.direction`);
	if (direction !== 'lower_better' && direction !== 'higher_better') {
		throw new Error(
			`lyrics api: kpis.${key}.direction must be lower_better or higher_better, got '${direction}'`
		);
	}
	return {
		label: _asString(obj.label, `kpis.${key}.label`),
		unit: _asString(obj.unit, `kpis.${key}.unit`),
		direction,
		title: _asString(obj.title, `kpis.${key}.title`)
	};
}

function _parseSnapshot(raw: unknown, index: number): KpiSnapshot {
	const obj = _asObject(raw, `snapshots[${index}]`);
	const valuesRaw = _asObject(obj.values, `snapshots[${index}].values`);
	const values: Record<string, number | null> = {};
	for (const [key, value] of Object.entries(valuesRaw)) {
		values[key] = _asNumberOrNull(value, `snapshots[${index}].values.${key}`);
	}
	const notes = _asStringOrNull(obj.notes, `snapshots[${index}].notes`);
	// The lyrics ledger stamps ONE provenance string per snapshot; KpiTile
	// wants a per-metric map (the demucs shape). Fan the string out over the
	// snapshot's value keys so 'hand' still badges as typed on every tile.
	const provenance: Record<string, string> = {};
	if (typeof obj.provenance === 'string') {
		for (const key of Object.keys(values)) provenance[key] = obj.provenance;
	} else if (obj.provenance !== undefined && obj.provenance !== null) {
		const raw = _asObject(obj.provenance, `snapshots[${index}].provenance`);
		for (const [key, origin] of Object.entries(raw)) {
			provenance[key] = _asString(origin, `snapshots[${index}].provenance.${key}`);
		}
	}
	return {
		ts: _asString(obj.ts, `snapshots[${index}].ts`),
		label: _asString(obj.label, `snapshots[${index}].label`),
		values,
		provenance,
		notes
	};
}

export async function fetchLyricsKpiLedger(): Promise<KpiLedger> {
	const raw = await _requestJson('GET', '/api/v1/bench/lyrics-kpi');
	const obj = _asObject(raw, 'response');
	const kpisRaw = _asObject(obj.kpis, 'kpis');
	const snapshotsRaw = _asArray(obj.snapshots, 'snapshots');
	if (snapshotsRaw.length === 0) throw new Error('lyrics api: snapshots is empty');
	const kpis: Record<string, KpiDef> = {};
	for (const [key, value] of Object.entries(kpisRaw)) kpis[key] = _parseKpiDef(value, key);
	return { kpis, snapshots: snapshotsRaw.map(_parseSnapshot) };
}

//----- source-order config ---------------------------------------------------

function _parseConfig(raw: unknown): LyricsConfig {
	const obj = _asObject(raw, 'config');
	const orderRaw = _asArray(obj.source_order, 'config.source_order');
	if (orderRaw.length === 0) throw new Error('lyrics api: config.source_order is empty');
	const source_order = orderRaw.map((v, i) => _asString(v, `config.source_order[${i}]`));
	const titlesRaw = _asObject(obj.source_titles, 'config.source_titles');
	const source_titles: Record<string, string> = {};
	for (const [key, title] of Object.entries(titlesRaw)) {
		source_titles[key] = _asString(title, `config.source_titles.${key}`);
	}
	return {
		source_order,
		source_titles,
		is_default: _asBoolean(obj.is_default, 'config.is_default')
	};
}

export async function fetchLyricsConfig(): Promise<LyricsConfig> {
	return _parseConfig(await _requestJson('GET', '/api/v1/lyrics/config'));
}

/** Persist a full reordering. A partial list 422s server-side; that message
 *  is surfaced verbatim so the operator sees exactly which source is missing. */
export async function saveLyricsConfig(source_order: string[]): Promise<LyricsConfig> {
	return _parseConfig(await _requestJson('PUT', '/api/v1/lyrics/config', { source_order }));
}

//----- triage listing --------------------------------------------------------

function _parseVerdict(raw: unknown, ctx: string): LyricVerdict {
	const obj = _asObject(raw, ctx);
	return {
		stable_id: _asString(obj.stable_id, `${ctx}.stable_id`),
		verdict: _asVerdictValue(obj.verdict, `${ctx}.verdict`),
		effective_verdict: _asVerdictValue(
			obj.effective_verdict ?? obj.effective,
			`${ctx}.effective_verdict`
		),
		effective: _asVerdictValue(obj.effective ?? obj.effective_verdict, `${ctx}.effective`),
		coverage_pct: _asNumberOrNull(obj.coverage_pct, `${ctx}.coverage_pct`),
		source: _asStringOrNull(obj.source, `${ctx}.source`),
		language_iso3: _asStringOrNull(obj.language_iso3, `${ctx}.language_iso3`),
		n_words: _asNumberOrNull(obj.n_words, `${ctx}.n_words`),
		n_lines: _asNumberOrNull(obj.n_lines, `${ctx}.n_lines`),
		has_words: _asBoolean(obj.has_words, `${ctx}.has_words`),
		pct_witness_red: _asNumberOrNull(obj.pct_witness_red, `${ctx}.pct_witness_red`),
		override:
			obj.override === null || obj.override === undefined
				? null
				: _asVerdictValue(obj.override, `${ctx}.override`),
		override_note: _asStringOrNull(obj.override_note, `${ctx}.override_note`),
		pipeline_version: _asString(obj.pipeline_version, `${ctx}.pipeline_version`),
		computed_at: _asString(obj.computed_at, `${ctx}.computed_at`),
		updated_at: _asString(obj.updated_at, `${ctx}.updated_at`),
		title: _asStringOrNull(obj.title, `${ctx}.title`),
		artist: _asStringOrNull(obj.artist, `${ctx}.artist`)
	};
}

export interface TriageParams {
	limit?: number;
	offset?: number;
	verdict?: string;
	order?: 'suspect' | 'coverage' | 'recent';
}

export async function fetchLyricTriage(params: TriageParams = {}): Promise<LyricVerdict[]> {
	const qs = Object.entries(params)
		.filter(([, v]) => v !== undefined && v !== null && v !== '')
		.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
		.join('&');
	const raw = await _requestJson('GET', `/api/v1/lyrics${qs ? '?' + qs : ''}`);
	return _asArray(raw, 'triage response').map((row, i) => _parseVerdict(row, `rows[${i}]`));
}

//----- summary ---------------------------------------------------------------

function _parseSummary(raw: unknown): LyricSummary {
	const obj = _asObject(raw, 'summary');
	const countsRaw = _asObject(obj.counts, 'summary.counts');
	const counts: Record<string, number> = {};
	for (const [key, value] of Object.entries(countsRaw)) {
		counts[key] = _asNumber(value, `summary.counts.${key}`);
	}
	return {
		counts,
		total: _asNumber(obj.total, 'summary.total'),
		no_lyrics_max_coverage: _asNumber(
			obj.no_lyrics_max_coverage,
			'summary.no_lyrics_max_coverage'
		),
		sparse_max_coverage: _asNumber(obj.sparse_max_coverage, 'summary.sparse_max_coverage')
	};
}

export async function fetchLyricSummary(): Promise<LyricSummary> {
	return _parseSummary(await _requestJson('GET', '/api/v1/lyrics/summary'));
}

//----- job queue -------------------------------------------------------------

function _parseJob(raw: unknown, ctx: string): LyricJob {
	const obj = _asObject(raw, ctx);
	const kind = _asString(obj.kind, `${ctx}.kind`);
	if (!JOB_KINDS.includes(kind as LyricJob['kind'])) {
		throw new Error(`lyrics api: ${ctx}.kind must be one of ${JOB_KINDS.join('|')}, got '${kind}'`);
	}
	const status = _asString(obj.status, `${ctx}.status`);
	if (!JOB_STATUSES.includes(status as LyricJob['status'])) {
		throw new Error(
			`lyrics api: ${ctx}.status must be one of ${JOB_STATUSES.join('|')}, got '${status}'`
		);
	}
	return {
		id: _asString(obj.id, `${ctx}.id`),
		ts: _asString(obj.ts, `${ctx}.ts`),
		kind: kind as LyricJob['kind'],
		stable_ids: _asArray(obj.stable_ids, `${ctx}.stable_ids`).map((v, i) =>
			_asString(v, `${ctx}.stable_ids[${i}]`)
		),
		status: status as LyricJob['status'],
		note: _asStringOrNull(obj.note, `${ctx}.note`)
	};
}

/** Newest first (the server reverses before serving). */
export async function fetchLyricJobs(): Promise<LyricJob[]> {
	const raw = await _requestJson('GET', '/api/v1/lyrics/jobs');
	return _asArray(raw, 'jobs response').map((job, i) => _parseJob(job, `jobs[${i}]`));
}

/** Queue a processing request. Queueing RECORDS the request; the offline
 *  stem/alignment farm drains it later - nothing runs in the daemon. */
export async function queueLyricJob(
	kind: LyricJob['kind'],
	stable_ids: string[],
	note?: string
): Promise<LyricJob> {
	const raw = await _requestJson('POST', '/api/v1/lyrics/jobs', {
		kind,
		stable_ids,
		note: note ?? null
	});
	return _parseJob(raw, 'job');
}
