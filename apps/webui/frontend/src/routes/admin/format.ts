/**
 * Value formatting for the KPI panel.
 *
 * Durations are the interesting case: a raw "1404 s" is unreadable, so any KPI
 * whose unit is seconds renders as "23m 24s". Sub-minute readings stay in
 * seconds with one decimal (11.1s, 5.9s) because that is the resolution the
 * bench actually measures at, and rounding 5.9s to "6s" would hide the gap
 * between a cold container and a fanned-out one.
 */

/** Unit string the ledger uses for a duration KPI. */
const DURATION_UNIT = 's';

export function isDuration(unit: string): boolean {
	return unit === DURATION_UNIT;
}

/** 1404 -> "23m 24s"; 265 -> "4m 25s"; 11.1 -> "11.1s"; 0 -> "0.0s". */
export function formatDuration(seconds: number): string {
	const abs = Math.abs(seconds);
	const sign = seconds < 0 ? '-' : '';
	if (abs < 60) return `${sign}${abs.toFixed(1)}s`;

	const total = Math.round(abs);
	const hours = Math.floor(total / 3600);
	const minutes = Math.floor((total % 3600) / 60);
	const secs = total % 60;
	if (hours > 0) return `${sign}${hours}h ${minutes}m ${secs}s`;
	return `${sign}${minutes}m ${secs}s`;
}

/** Non-duration numbers: keep small costs precise, group thousands. */
export function formatNumber(value: number): string {
	const abs = Math.abs(value);
	if (abs !== 0 && abs < 0.01) return value.toPrecision(2);
	if (abs >= 1000) return value.toLocaleString('en-US');
	if (Number.isInteger(value)) return String(value);
	return String(Math.round(value * 100) / 100);
}

/**
 * Display string for a reading. Durations carry their own unit, so callers
 * must not append `unit` again when {@link isDuration} is true.
 */
export function formatValue(value: number, unit: string): string {
	return isDuration(unit) ? formatDuration(value) : formatNumber(value);
}

/** Same as {@link formatValue} but always signed, for deltas. */
export function formatDelta(delta: number, unit: string): string {
	if (isDuration(unit)) {
		const magnitude = formatDuration(Math.abs(delta));
		return delta > 0 ? `+${magnitude}` : delta < 0 ? `-${magnitude}` : magnitude;
	}
	const magnitude = formatNumber(Math.abs(delta));
	return delta > 0 ? `+${magnitude}` : delta < 0 ? `-${magnitude}` : magnitude;
}

/** Value plus its unit, for prose. Durations already read as a duration. */
export function formatWithUnit(value: number, unit: string): string {
	return isDuration(unit) ? formatDuration(value) : `${formatNumber(value)} ${unit}`;
}

export function formatDeltaWithUnit(delta: number, unit: string): string {
	return isDuration(unit) ? formatDelta(delta, unit) : `${formatDelta(delta, unit)} ${unit}`;
}

//----- direction judgement --------------------------------------------------

export type Verdict = 'good' | 'bad' | 'flat';

/** Did `delta` move the way this KPI wants? Unknown directions are a hard error. */
export function judge(delta: number, direction: string): Verdict {
	if (delta === 0) return 'flat';
	if (direction === 'higher_better') return delta > 0 ? 'good' : 'bad';
	if (direction === 'lower_better') return delta > 0 ? 'bad' : 'good';
	throw new Error(`kpi: direction must be higher_better or lower_better, got '${direction}'`);
}

export const ARROW: Record<Verdict, string> = { good: '▲', bad: '▼', flat: '▬' };
