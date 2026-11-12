/**
 * Visible list track count and summed duration (rekordbox library footer parity).
 *
 * Mirror of apps/shared/list_totals.py for the browser footer; agents use the
 * HTTP route and CLI instead of this module.
 */

export interface DurationRow {
	duration_ms: number | null;
}

export interface ListTotals {
	count: number;
	duration_ms: number;
	unknown: number;
}

/** One pass over rows: count every row, sum known durations, tally nulls. */
export function summarizeListTotals(rows: readonly DurationRow[]): ListTotals {
	const count = rows.length;
	let duration_ms = 0;
	let unknown = 0;
	for (let i = 0; i < count; i += 1) {
		const d = rows[i]!.duration_ms;
		if (d === null || !Number.isFinite(d) || d < 0) {
			unknown += 1;
			continue;
		}
		duration_ms += d;
	}
	return { count, duration_ms, unknown };
}

/** M:SS under an hour, H:MM:SS from an hour up (rekordbox-style). */
export function formatDurationHms(ms: number): string {
	const total = Math.max(0, Math.round(ms / 1000));
	const h = Math.floor(total / 3600);
	const m = Math.floor((total % 3600) / 60);
	const s = String(total % 60).padStart(2, '0');
	if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${s}`;
	return `${m}:${s}`;
}

export function formatListTotalsLine(totals: ListTotals): string {
	const label = totals.count === 1 ? 'track' : 'tracks';
	const base = `${totals.count} ${label}, ${formatDurationHms(totals.duration_ms)}`;
	if (totals.unknown > 0) {
		return `${base} (+${totals.unknown} unknown)`;
	}
	return base;
}

export function formatSelectionSuffix(totals: ListTotals): string {
	if (totals.count < 2) return '';
	const label = totals.count === 1 ? 'track' : 'tracks';
	const unk = totals.unknown > 0 ? ` (+${totals.unknown} unknown)` : '';
	return `selected: ${totals.count} ${label}, ${formatDurationHms(totals.duration_ms)}${unk}`;
}

export function formatLibraryFooter(
	listTotals: ListTotals,
	selectionTotals: ListTotals | null
): string {
	const line = formatListTotalsLine(listTotals);
	if (selectionTotals === null || selectionTotals.count < 2) return line;
	return `${line} · ${formatSelectionSuffix(selectionTotals)}`;
}
