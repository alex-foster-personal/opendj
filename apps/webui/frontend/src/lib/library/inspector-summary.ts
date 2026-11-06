/**
 * LIBWS-02: pure readouts for the Library workspace track inspector.
 *
 * A multi-row selection shows one value per field when every row agrees,
 * and "Multiple values" when they do not (LIBM-63: mixed is a readout,
 * never an input value). An empty field stays empty; it is never filled
 * with a guess.
 */

import { normalizeRating } from '$lib/rating';

export type FieldReadout<T> =
	| { kind: 'empty' }
	| { kind: 'same'; value: T }
	| { kind: 'mixed'; distinct: number };

export const MIXED_LABEL = 'Multiple values';

/** One readout for a field across the selection. null/undefined/'' count as empty. */
export function readField<T>(values: ReadonlyArray<T | null | undefined>): FieldReadout<T> {
	const present: T[] = [];
	for (const value of values) {
		if (value === null || value === undefined) continue;
		if (typeof value === 'string' && value.trim() === '') continue;
		present.push(value);
	}
	if (present.length === 0) return { kind: 'empty' };
	const distinct = new Set(present.map((value) => JSON.stringify(value)));
	if (distinct.size === 1 && present.length === values.length) {
		return { kind: 'same', value: present[0] as T };
	}
	// Some rows have a value and some do not, or the values differ: both read
	// as mixed so a partial field is never shown as if it covered every row.
	return { kind: 'mixed', distinct: distinct.size + (present.length === values.length ? 0 : 1) };
}

/** Text for a readout; `format` renders a single agreed value. */
export function readoutText<T>(
	readout: FieldReadout<T>,
	format: (value: T) => string = (value) => String(value)
): string {
	if (readout.kind === 'empty') return '';
	if (readout.kind === 'mixed') return MIXED_LABEL;
	return format(readout.value);
}

export interface InspectorRow {
	stable_id: string;
	title: string | null;
	artist: string | null;
	genre: string | null;
	key: string | null;
	bpm: number | null;
	rating: number | null;
	energy: number | null;
	comments: string | null;
	duration_ms: number | null;
}

export interface SelectionSummary {
	count: number;
	/** Sum of known durations; null when no row has a duration. */
	total_duration_ms: number | null;
	/** Rows whose duration is unknown, so the total is honest about its gaps. */
	unknown_duration_count: number;
	bpm_min: number | null;
	bpm_max: number | null;
	keys: string[];
	genre: FieldReadout<string>;
	artist: FieldReadout<string>;
	rating: FieldReadout<number>;
	comments: FieldReadout<string>;
}

export function summarizeSelection(rows: ReadonlyArray<InspectorRow>): SelectionSummary {
	let total = 0;
	let known = 0;
	let bpmMin: number | null = null;
	let bpmMax: number | null = null;
	const keys = new Set<string>();
	for (const row of rows) {
		if (row.duration_ms !== null && Number.isFinite(row.duration_ms)) {
			total += row.duration_ms;
			known += 1;
		}
		if (row.bpm !== null && Number.isFinite(row.bpm)) {
			bpmMin = bpmMin === null ? row.bpm : Math.min(bpmMin, row.bpm);
			bpmMax = bpmMax === null ? row.bpm : Math.max(bpmMax, row.bpm);
		}
		if (row.key !== null && row.key !== '') keys.add(row.key);
	}
	return {
		count: rows.length,
		total_duration_ms: known === 0 ? null : total,
		unknown_duration_count: rows.length - known,
		bpm_min: bpmMin,
		bpm_max: bpmMax,
		keys: [...keys].sort(compareCamelot),
		genre: readField(rows.map((row) => row.genre)),
		artist: readField(rows.map((row) => row.artist)),
		rating: readField(rows.map((row) => normalizeRating(row.rating))),
		comments: readField(rows.map((row) => row.comments))
	};
}

/** Camelot order (1A, 1B, 2A ... 12B); anything unparsed sorts last, by text. */
export function compareCamelot(a: string, b: string): number {
	const pa = /^(\d{1,2})([AB])$/i.exec(a.trim());
	const pb = /^(\d{1,2})([AB])$/i.exec(b.trim());
	if (pa && pb) {
		const na = Number(pa[1]);
		const nb = Number(pb[1]);
		if (na !== nb) return na - nb;
		return pa[2].toUpperCase().localeCompare(pb[2].toUpperCase());
	}
	if (pa) return -1;
	if (pb) return 1;
	return a.localeCompare(b);
}

/** m:ss for a track, h:mm:ss once a selection runs past an hour. */
export function formatDuration(ms: number): string {
	const totalSeconds = Math.max(0, Math.round(ms / 1000));
	const hours = Math.floor(totalSeconds / 3600);
	const minutes = Math.floor((totalSeconds % 3600) / 60);
	const seconds = totalSeconds % 60;
	const ss = String(seconds).padStart(2, '0');
	if (hours > 0) return `${hours}:${String(minutes).padStart(2, '0')}:${ss}`;
	return `${minutes}:${ss}`;
}

export function formatBpmRange(min: number | null, max: number | null): string {
	if (min === null || max === null) return '';
	if (min === max) return min.toFixed(2);
	return `${min.toFixed(2)} to ${max.toFixed(2)}`;
}

/**
 * Who set a field, for the inspector's source chip. Keys are the provenance
 * writers the engine stamps (`own-analysis` is apps/analysis/selection.py's
 * OWN_ANALYSIS_SOURCE). A writer this table does
 * not know keeps its own name in upper case rather than being folded into a
 * familiar label it did not come from.
 */
const SOURCE_LABELS: Readonly<Record<string, string>> = {
	rekordbox: 'RB',
	mik: 'MIK',
	djay: 'DJAY',
	serato: 'SERATO',
	traktor: 'TRAKTOR',
	'open-dj-tool': 'OWN',
	'own-analysis': 'OWN',
	manual: 'YOU',
	webui: 'YOU',
	inferred: 'INFERRED'
};

export function sourceLabel(source: string): string {
	const key = source.trim().toLowerCase();
	if (key === '') throw new Error('provenance source is empty');
	return SOURCE_LABELS[key] ?? key.toUpperCase();
}
