/**
 * Library Camelot key wheel: geometry, persisted multi-select, exact-match
 * filtering, and default compatibility expansion (issue #7599).
 */
import { camelotRulesAllow, parseCamelotKey, type CamelotKey } from '$lib/player/key/camelot';
import {
	CAMELOT_MUSICAL_SEPARATOR,
	displayKey,
	type KeyNotation
} from '$lib/rb/key-notation';
import { COMPATIBLE_FILTER_DEFAULTS } from '$lib/rb/compatible-filter-prefs';
import { camelotKeyColor } from '$lib/rb/camelot-color';

const CANONICAL_CODE_RE = /^(?:1[0-2]|[1-9])[AB]$/;

export function isCanonicalCamelotCode(code: string): boolean {
	return CANONICAL_CODE_RE.test(code);
}

/** Wheel iteration order: outer B then inner A for each clock number 1..12. */
export const LIBRARY_KEY_WHEEL_ORDER: readonly string[] = (() => {
	const codes: string[] = [];
	for (let number = 1; number <= 12; number += 1) {
		codes.push(`${number}B`, `${number}A`);
	}
	return codes;
})();

/** Segment fill: exactly the track table's Key swatch (camelotKeyColor),
 * including the skin's lightness variables, so the wheel matches the table in
 * every skin. Apply it as a CSS `fill` style, not an attribute, so the
 * variables resolve (Sol P1, #7743). */
export function libraryKeySegmentFill(number: number, mode: CamelotKey['mode']): string {
	const color = camelotKeyColor(`${number}${mode}`);
	if (color === null) throw new Error(`libraryKeySegmentFill: no swatch for ${number}${mode}`);
	return color;
}

/** Selected keys stay bright and the rest dim. With nothing selected every
 * segment is bright, because nothing is being filtered. */
export const LIBRARY_KEY_DIM_OPACITY = 0.22;
export function libraryKeySegmentOpacity(selected: boolean, anySelected: boolean): number {
	return !anySelected || selected ? 1 : LIBRARY_KEY_DIM_OPACITY;
}

export const LIBRARY_KEY_WHEEL_SIZE = 260;
const CENTER = LIBRARY_KEY_WHEEL_SIZE / 2;
const RINGS: Readonly<Record<CamelotKey['mode'], { outer: number; inner: number }>> = {
	B: { outer: 112, inner: 84 },
	A: { outer: 82, inner: 54 }
};

export interface LibraryKeyWheelSegment {
	code: string;
	number: number;
	mode: CamelotKey['mode'];
	path: string;
	labelX: number;
	labelY: number;
}

function _round(value: number): number {
	return Math.round(value * 100) / 100;
}

function _point(radius: number, degrees: number): string {
	const radians = (degrees * Math.PI) / 180;
	return `${_round(CENTER + radius * Math.cos(radians))} ${_round(CENTER + radius * Math.sin(radians))}`;
}

/** Angular gap between neighbouring segments, in degrees. */
const SEGMENT_GAP_DEG = 1.2;

function _sectorPath(number: number, outer: number, inner: number): string {
	const start = number * 30 - 105 + SEGMENT_GAP_DEG / 2;
	const end = start + 30 - SEGMENT_GAP_DEG;
	return (
		`M${_point(outer, start)} A${outer} ${outer} 0 0 1 ${_point(outer, end)}` +
		` L${_point(inner, end)} A${inner} ${inner} 0 0 0 ${_point(inner, start)} Z`
	);
}

export function libraryKeyWheelSegments(): LibraryKeyWheelSegment[] {
	const segments: LibraryKeyWheelSegment[] = [];
	for (let number = 1; number <= 12; number += 1) {
		for (const mode of ['B', 'A'] as const) {
			const { outer, inner } = RINGS[mode];
			const middle = (outer + inner) / 2;
			const [labelX, labelY] = _point(middle, number * 30 - 90).split(' ').map(Number);
			segments.push({
				code: `${number}${mode}`,
				number,
				mode,
				path: _sectorPath(number, outer, inner),
				labelX,
				labelY
			});
		}
	}
	return segments;
}

export function canonicalizeLibraryKeyFilter(keys: readonly string[]): string[] {
	const selected = new Set<string>();
	for (const key of keys) {
		if (!isCanonicalCamelotCode(key)) {
			throw new Error(`library_key_filter: invalid canonical code ${JSON.stringify(key)}`);
		}
		if (selected.has(key)) {
			throw new Error(`library_key_filter: duplicate code ${key}`);
		}
		selected.add(key);
	}
	if (selected.size > 24) {
		throw new Error(`library_key_filter: at most 24 keys, got ${selected.size}`);
	}
	return LIBRARY_KEY_WHEEL_ORDER.filter((code) => selected.has(code));
}

export function validateLibraryKeyFilterField(
	raw: unknown,
	storageKey: string
): string[] {
	if (raw === undefined) return [];
	if (!Array.isArray(raw)) {
		throw new Error(`${storageKey}: library_key_filter must be an array`);
	}
	try {
		return canonicalizeLibraryKeyFilter(raw);
	} catch (error: unknown) {
		const detail = error instanceof Error ? error.message : String(error);
		throw new Error(`${storageKey}: ${detail}`);
	}
}

export function toggleLibraryKeySelection(
	current: readonly string[],
	code: string
): string[] {
	if (!isCanonicalCamelotCode(code)) {
		throw new Error(`toggleLibraryKeySelection: invalid code ${JSON.stringify(code)}`);
	}
	const set = new Set(current);
	if (set.has(code)) set.delete(code);
	else set.add(code);
	return LIBRARY_KEY_WHEEL_ORDER.filter((c) => set.has(c));
}

export function resetLibraryKeySelection(): string[] {
	return [];
}

export function rowMatchesLibraryKeyFilter(
	rowKey: string | null,
	selectedKeys: readonly string[]
): boolean {
	if (selectedKeys.length === 0) return true;
	const parsed = parseCamelotKey(rowKey);
	if (parsed === null) return false;
	const code = `${parsed.number}${parsed.mode}`;
	return selectedKeys.includes(code);
}

/** The hard veto: with keys selected, only rows in those keys survive. */
export function applyLibraryKeyFilter<T extends { key: string | null }>(
	rows: T[],
	selectedKeys: readonly string[]
): T[] {
	if (selectedKeys.length === 0) return rows;
	return rows.filter((row) => rowMatchesLibraryKeyFilter(row.key, selectedKeys));
}

/** The Compatible button's selection: the reference key, its number +/- 1 in
 * the same ring, and its A/B swap. These are the default-on Camelot rules
 * (same_key, adjacent, relative) from compatible-filter-prefs.ts, evaluated by
 * the shared rule table (camelotRulesAllow), not another copy of wheel maths.
 * The user's own compatible-filter rule toggles do not change it: the button
 * means the approved neighbour set. */
export function compatibleLibraryKeys(reference: string): string[] {
	if (!isCanonicalCamelotCode(reference)) {
		throw new Error(`compatibleLibraryKeys: invalid reference ${JSON.stringify(reference)}`);
	}
	return LIBRARY_KEY_WHEEL_ORDER.filter((candidate) =>
		camelotRulesAllow(reference, candidate, COMPATIBLE_FILTER_DEFAULTS.rules)
	);
}

export interface LibraryKeyFilterDeckSlice {
	is_master: boolean;
	playing: boolean;
	key: string | null;
}

/** Selected row key wins; else a playing deck with a parseable key (master first). */
export function resolveLibraryKeyFilterReference(
	selectedRowKey: string | null,
	decks: readonly LibraryKeyFilterDeckSlice[]
): string | null {
	const selected = parseCamelotKey(selectedRowKey);
	if (selected !== null) return `${selected.number}${selected.mode}`;
	const playing = decks.filter((d) => d.playing);
	const ordered = [
		...playing.filter((d) => d.is_master),
		...playing.filter((d) => !d.is_master)
	];
	for (const deck of ordered) {
		const parsed = parseCamelotKey(deck.key);
		if (parsed !== null) return `${parsed.number}${parsed.mode}`;
	}
	return null;
}

/** Drops a disk read of the key filter that a newer read or a newer local
 * edit has superseded, so an out-of-order GET can never install an older
 * selection (Codex P2, #7743). `writeRev` counts local edits. */
export function makeKeyFilterReadGuard(writeRev: () => number): {
	begin: () => number;
	isCurrent: (token: number) => boolean;
} {
	let latestRead = 0;
	const revAtRead = new Map<number, number>();
	return {
		begin() {
			latestRead += 1;
			revAtRead.clear();
			revAtRead.set(latestRead, writeRev());
			return latestRead;
		},
		isCurrent(token) {
			return token === latestRead && revAtRead.get(token) === writeRev();
		}
	};
}

export interface LibraryKeyWheelLabelLines {
	line1: string;
	line2?: string;
}

export function libraryKeyWheelLabelLines(
	code: string,
	notation: KeyNotation
): LibraryKeyWheelLabelLines {
	const shown = displayKey(code, notation);
	// Every wheel code is canonical, so an unformatted one is a formatter fault (Sol P1, #7743).
	if (shown === null || !shown.parsed) {
		throw new Error(`libraryKeyWheelLabelLines: ${code} did not format in ${notation}`);
	}
	if (notation === 'camelot_musical') {
		const bullet = shown.text.indexOf(CAMELOT_MUSICAL_SEPARATOR);
		if (bullet === -1) {
			throw new Error(`libraryKeyWheelLabelLines: ${JSON.stringify(shown.text)} has no Camelot/musical separator`);
		}
		return {
			line1: shown.text.slice(0, bullet).trim(),
			line2: shown.text.slice(bullet + CAMELOT_MUSICAL_SEPARATOR.length).trim()
		};
	}
	return { line1: shown.text };
}

export function libraryKeyWheelAriaLabel(code: string, notation: KeyNotation): string {
	const lines = libraryKeyWheelLabelLines(code, notation);
	const formatted = lines.line2 === undefined ? lines.line1 : `${lines.line1}, ${lines.line2}`;
	return `${formatted} (${code})`;
}
