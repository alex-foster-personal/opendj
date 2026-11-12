/**
 * BACKSTAGE-07: the row model behind the Backstage KPI tables (/admin?p=perf
 * and ?p=farm with shell.backstage on). Pure, so ordering, filtering, the move
 * wording and the staleness rule are unit tested
 * (tests/unit/backstage-kpi-table.test.mjs) without a DOM.
 *
 * Built on the same reading model as the tiles (buildKpiCard), so a table row
 * and a tile can never disagree about the latest reading or the verdict.
 */

import { formatValue, formatWithUnit, isDuration, MOVE_ARROW, type Verdict } from './format';
import type { KpiDef, KpiSnapshot } from './kpi-api';
import { buildKpiCard, type KpiCardModel } from './kpi-card';
import { calendarDays, snapshotDay } from './spark-geometry';
import { STALE_KPI_DAYS } from './status-rows';

/** At or above this percentage a move reads as a multiple ("x12"). */
export const MULTIPLE_PCT = 1000;

export interface KpiMove {
	/** e.g. "up 42% worse", "down x12 better" with arrows, "no change", "one reading". */
	text: string;
	verdict: Verdict | 'none';
	/** Absolute percent move; Infinity when the previous reading was 0; 0 when unmoved. */
	magnitude: number;
	/** Hover title with the raw numbers. */
	title: string;
}

export interface KpiRow {
	metric: string;
	kpi: KpiDef;
	card: KpiCardModel;
	latest: number | null;
	latestText: string;
	latestTitle: string;
	move: KpiMove;
	ageDays: number | null;
	stale: boolean;
	measuredDay: string | null;
}

/** Direction-aware move wording. Giant percentages read as "xN". */
export function describeMove(
	previous: number | null,
	latest: number | null,
	verdict: Verdict,
	unit: string
): KpiMove {
	if (previous === null || latest === null) {
		return {
			text: 'one reading',
			verdict: 'none',
			magnitude: 0,
			title: 'Only one reading so far: nothing to compare against.'
		};
	}
	if (previous === latest) {
		return {
			text: 'no change',
			verdict: 'flat',
			magnitude: 0,
			title: `Unchanged at ${formatWithUnit(latest, unit)}.`
		};
	}
	const arrow = latest > previous ? MOVE_ARROW.up : MOVE_ARROW.down;
	const word = verdict === 'good' ? 'better' : 'worse';
	const raw = `From ${formatWithUnit(previous, unit)} to ${formatWithUnit(latest, unit)}.`;
	if (previous === 0) {
		return {
			text: `${arrow} from 0 ${word}`,
			verdict,
			magnitude: Infinity,
			title: `${raw} A move from zero has no percentage.`
		};
	}
	const pct = (Math.abs(latest - previous) / Math.abs(previous)) * 100;
	let size: string;
	if (pct >= MULTIPLE_PCT) {
		const ratio =
			latest !== 0
				? Math.max(Math.abs(latest / previous), Math.abs(previous / latest))
				: Math.abs(pct / 100);
		size = `x${Math.round(ratio)}`;
	} else if (pct >= 1) {
		size = `${Math.round(pct)}%`;
	} else {
		size = '<1%';
	}
	return {
		text: `${arrow} ${size} ${word}`,
		verdict,
		magnitude: pct,
		title: `${raw} ${pct.toFixed(1)}% move.`
	};
}

export function buildKpiRow(
	metric: string,
	kpi: KpiDef,
	snapshots: KpiSnapshot[],
	today: string
): KpiRow {
	const card = buildKpiCard(metric, kpi, snapshots);
	const latest = card.latest?.value ?? null;
	const previous = card.previous?.value ?? null;
	const measuredDay = card.latest ? snapshotDay(snapshots[card.latest.index].ts) : null;
	const ageDays = measuredDay !== null ? Math.max(0, calendarDays(measuredDay, today)) : null;
	const value = latest === null ? '-' : formatValue(latest, kpi.unit);
	return {
		metric,
		kpi,
		card,
		latest,
		latestText: latest === null || isDuration(kpi.unit) ? value : `${value} ${kpi.unit}`,
		latestTitle:
			latest === null
				? 'Never measured.'
				: `${kpi.label}: ${formatWithUnit(latest, kpi.unit)}, from ${snapshots[card.latest!.index].label} (${snapshots[card.latest!.index].ts}).`,
		move: describeMove(previous, latest, card.verdict, kpi.unit),
		ageDays,
		stale: ageDays !== null && ageDays > STALE_KPI_DAYS,
		measuredDay
	};
}

/**
 * Rows that moved come first, largest absolute move first; unmoved rows keep
 * ledger order after them. Stable.
 */
export function orderKpiRows(rows: KpiRow[]): KpiRow[] {
	return rows
		.map((row, i) => ({ row, i }))
		.sort((a, b) => {
			const am = a.row.move.magnitude;
			const bm = b.row.move.magnitude;
			if (am !== bm) return bm > am ? 1 : -1;
			return a.i - b.i;
		})
		.map((x) => x.row);
}

/** Case-insensitive match on label or metric key; blank matches everything. */
export function filterKpiRows(rows: KpiRow[], query: string): KpiRow[] {
	const q = query.trim().toLowerCase();
	if (!q) return rows;
	return rows.filter(
		(r) => r.kpi.label.toLowerCase().includes(q) || r.metric.toLowerCase().includes(q)
	);
}

export interface KpiTable {
	/** Measured rows, ordered. */
	rows: KpiRow[];
	/** Never-measured rows, folded into one line. */
	never: KpiRow[];
}

export function buildKpiTable(
	kpis: Record<string, KpiDef>,
	snapshots: KpiSnapshot[],
	today: string,
	query = ''
): KpiTable {
	const all = Object.entries(kpis).map(([metric, kpi]) =>
		buildKpiRow(metric, kpi, snapshots, today)
	);
	const shown = filterKpiRows(all, query);
	return {
		rows: orderKpiRows(shown.filter((r) => r.latest !== null)),
		never: shown.filter((r) => r.latest === null)
	};
}

export interface LedgerStaleness {
	lastDay: string | null;
	lastLabel: string | null;
	ageDays: number | null;
	stale: boolean;
}

/** Newest snapshot date and how old it is; stale past STALE_KPI_DAYS. */
export function ledgerStaleness(snapshots: KpiSnapshot[], today: string): LedgerStaleness {
	let last: KpiSnapshot | null = null;
	for (const s of snapshots) {
		if (last === null || snapshotDay(s.ts) >= snapshotDay(last.ts)) last = s;
	}
	if (last === null) return { lastDay: null, lastLabel: null, ageDays: null, stale: false };
	const lastDay = snapshotDay(last.ts);
	const ageDays = Math.max(0, calendarDays(lastDay, today));
	return { lastDay, lastLabel: last.label, ageDays, stale: ageDays > STALE_KPI_DAYS };
}
