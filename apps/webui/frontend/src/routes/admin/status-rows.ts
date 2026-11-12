/**
 * Backstage Status overview (BACKSTAGE-06): one row per area, derived from
 * the REAL endpoints the app already calls. Pure, so the derivation is unit
 * tested (tests/unit/backstage-status.test.mjs); StatusOverview.svelte does
 * the fetching and hands each answer in as a Probe.
 *
 * State shapes (mock admin-redesign.html bodyStatus): ok = filled dot,
 * warn = ring, err = triangle, idle = dashed square (no verdict yet, or
 * nothing to judge). An area with no data source at all is `inert` and
 * carries the PARITY-TODO tooltip; it is never given an invented value.
 */

import { NOT_IMPLEMENTED_TITLE } from '$lib/shell/backstage-nav';

export type StatusState = 'ok' | 'warn' | 'err' | 'idle';

/** One fetch's outcome. `loading` until it settles. */
export type Probe<T> =
	{ kind: 'loading' } | { kind: 'ok'; value: T } | { kind: 'error'; message: string };

/** A piece of a row's line: plain text, or a number with its hover title. */
export type Segment = string | { n: string; title: string };

export interface StatusRow {
	id: string;
	area: string;
	state: StatusState;
	line: Segment[];
	/** Where the row links: an /admin?p= panel or a page. */
	href: string;
	title: string;
}

export interface InertArea {
	id: string;
	area: string;
	title: string;
}

export interface StatusInputs {
	health: Probe<{ status: string; tracks: number; tracksPlayable: number }>;
	writeback: Probe<{ enabled: boolean; title: string }>;
	dedup: Probe<{ undecided: number; total: number; note: string | null }>;
	/** `warming`: the engine has not finished its first library scan. */
	missing: Probe<{ warming: true } | { warming: false; broken: number; total: number }>;
	/** Latest snapshot timestamps (ISO date or datetime); empty = never measured. */
	perf: Probe<{ latestTs: string | null; kpis: number }>;
	farm: Probe<{ latestTs: string | null; kpis: number }>;
	now: Date;
}

/** KPI data older than this many whole days is stale (warn). */
export const STALE_KPI_DAYS = 7;

const DAY_MS = 86_400_000;

/** Whole days between `ts` and `now`, or null when `ts` does not parse. */
export function ageDays(ts: string, now: Date): number | null {
	const t = Date.parse(ts);
	if (Number.isNaN(t)) return null;
	return Math.floor((now.getTime() - t) / DAY_MS);
}

const n = (value: number | string, title: string): Segment => ({ n: String(value), title });

function pending<T>(p: Probe<T>, row: Omit<StatusRow, 'state' | 'line'>): StatusRow | null {
	if (p.kind === 'loading') return { ...row, state: 'idle', line: ['Checking...'] };
	if (p.kind === 'error') return { ...row, state: 'err', line: [`Could not read: ${p.message}`] };
	return null;
}

function healthRow(p: StatusInputs['health']): StatusRow {
	const base = {
		id: 'health',
		area: 'Health',
		href: '/admin?p=health',
		title: 'GET /api/v1/health'
	};
	if (p.kind !== 'ok') return pending(p, base)!;
	const { status, tracks, tracksPlayable } = p.value;
	if (status !== 'ok')
		return { ...base, state: 'warn', line: [`Daemon reports status "${status}"`] };
	return {
		...base,
		state: 'ok',
		line: [
			'Daemon up, ',
			n(tracks, 'Tracks in state.db'),
			' tracks (',
			n(tracksPlayable, 'Tracks whose audio is playable on this machine'),
			' playable)'
		]
	};
}

function writebackRow(p: StatusInputs['writeback']): StatusRow {
	const base = {
		id: 'writeback',
		area: 'Flags',
		href: '/admin?p=flags',
		title: 'GET /api/v1/rekordbox/writeback-gate'
	};
	if (p.kind !== 'ok') return pending(p, base)!;
	if (!p.value.enabled) {
		return {
			...base,
			state: 'warn',
			title: p.value.title,
			line: ['Rekordbox writeback is refused: one-way import only']
		};
	}
	return { ...base, state: 'ok', line: ['Rekordbox writeback is enabled'] };
}

function dedupRow(p: StatusInputs['dedup']): StatusRow {
	const base = { id: 'dedup', area: 'Dedup', href: '/dedup', title: 'GET /api/v1/dedup/clusters' };
	if (p.kind !== 'ok') return pending(p, base)!;
	const { undecided, total, note } = p.value;
	if (total === 0 && note !== null)
		return { ...base, state: 'idle', title: note, line: ['No duplicate scan has run yet'] };
	if (undecided > 0) {
		return {
			...base,
			state: 'warn',
			line: [
				n(undecided, 'Duplicate clusters with no decision yet'),
				' of ',
				n(total, 'Duplicate clusters found by the last scan'),
				' duplicate clusters to review'
			]
		};
	}
	return { ...base, state: 'ok', line: ['No duplicate clusters waiting'] };
}

function missingRow(p: StatusInputs['missing']): StatusRow {
	const base = {
		id: 'missing',
		area: 'Missing tracks',
		href: '/reconcile',
		title: 'GET /api/v1/reconcile/summary?cached=true'
	};
	if (p.kind !== 'ok') return pending(p, base)!;
	const v = p.value;
	if (v.warming) return { ...base, state: 'idle', line: ['First library scan still running'] };
	if (v.broken > 0) {
		return {
			...base,
			state: 'warn',
			line: [
				n(v.broken, 'Tracks whose audio file cannot be found'),
				' of ',
				n(v.total, 'Tracks in the library (the denominator)'),
				' tracks missing their audio'
			]
		};
	}
	return { ...base, state: 'ok', line: ['No tracks missing their audio'] };
}

function kpiRow(
	p: StatusInputs['perf'],
	now: Date,
	id: 'perf' | 'farm',
	area: string,
	endpoint: string
): StatusRow {
	const base = { id, area, href: `/admin?p=${id}`, title: endpoint };
	if (p.kind !== 'ok') return pending(p, base)!;
	const { latestTs, kpis } = p.value;
	if (latestTs === null)
		return { ...base, state: 'warn', line: [`${area} data has never been measured`] };
	const age = ageDays(latestTs, now);
	if (age === null)
		return { ...base, state: 'err', line: [`Latest run date "${latestTs}" does not parse`] };
	const ageSeg = n(
		age,
		`Whole days since the latest run (${latestTs}); over ${STALE_KPI_DAYS} is stale`
	);
	const kpiSeg = n(kpis, `KPIs declared in the ledger`);
	if (age > STALE_KPI_DAYS) {
		return {
			...base,
			state: 'warn',
			line: [`${area} data is `, ageSeg, ' days old (', kpiSeg, ' KPIs)']
		};
	}
	return { ...base, state: 'ok', line: ['Latest run ', ageSeg, ' days ago (', kpiSeg, ' KPIs)'] };
}

/** Every area with a real data source, in display order. */
export function deriveStatusRows(input: StatusInputs): StatusRow[] {
	return [
		healthRow(input.health),
		writebackRow(input.writeback),
		missingRow(input.missing),
		dedupRow(input.dedup),
		kpiRow(input.perf, input.now, 'perf', 'Performance KPIs', 'GET /api/v1/bench/perf-kpi'),
		kpiRow(input.farm, input.now, 'farm', 'Stem farm', 'GET /api/v1/bench/kpi')
	];
}

/** Areas the Status page cannot judge yet: no endpoint reports their state. */
export const INERT_AREAS: readonly InertArea[] = [
	{ id: 'triage', area: 'Lyric human review', title: NOT_IMPLEMENTED_TITLE },
	{ id: 'stems', area: 'Stems', title: NOT_IMPLEMENTED_TITLE },
	{ id: 'vocals', area: 'Vocals', title: NOT_IMPLEMENTED_TITLE }
];

export interface StatusView {
	/** Rows that need attention (or have no verdict yet), worst first. */
	attention: StatusRow[];
	/** Healthy areas, collapsed into one "All clear" line. */
	clear: StatusRow[];
}

const RANK: Record<StatusState, number> = { err: 0, warn: 1, idle: 2, ok: 3 };

export function splitStatus(rows: readonly StatusRow[]): StatusView {
	const attention = rows.filter((r) => r.state !== 'ok');
	attention.sort((a, b) => RANK[a.state] - RANK[b.state]);
	return { attention, clear: rows.filter((r) => r.state === 'ok') };
}

/** "All clear: Health, Flags" or null when nothing is clear. */
export function allClearLine(clear: readonly StatusRow[]): string | null {
	return clear.length === 0 ? null : `All clear: ${clear.map((r) => r.area).join(', ')}`;
}

export const STATE_LABEL: Record<StatusState, string> = {
	ok: 'fine',
	warn: 'needs a look',
	err: 'broken',
	idle: 'no verdict yet'
};
