/** Classic Camelot wheel hues (1..12); A/B share the number colour. */
const CAMELOT_HUES = [0, 30, 55, 85, 120, 155, 185, 210, 240, 275, 300, 330] as const;

/** Pitch-class → display name (C=0). Mixed sharp/flat labels match common Camelot charts. */
const NOTE_NAMES = ['C', 'C#', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B'] as const;

/** Same root tables as audio-engine parseCamelotKey (1A=Ab minor … 12B=E major). */
const CAMELOT_ROOTS: Record<'A' | 'B', readonly number[]> = {
	A: [8, 3, 10, 5, 0, 7, 2, 9, 4, 11, 6, 1],
	B: [11, 6, 1, 8, 3, 10, 5, 0, 7, 2, 9, 4]
};

function _parseCamelotParts(key: string): { n: number; mode: 'A' | 'B' } | null {
	const match = /^(\d{1,2})\s*([ABab])$/.exec(key.trim());
	if (match === null) return null;
	const n = Number(match[1]);
	if (!Number.isInteger(n) || n < 1 || n > 12) return null;
	return { n, mode: match[2].toUpperCase() as 'A' | 'B' };
}

/** Colour for a Camelot key label, or null when the string is not Camelot. */
export function camelotKeyColor(key: string | null): string | null {
	if (key === null) return null;
	const parts = _parseCamelotParts(key);
	if (parts === null) return null;
	const hue = CAMELOT_HUES[parts.n - 1];
	const major = parts.mode === 'B';
	return major ? `hsl(${hue} 72% 62%)` : `hsl(${hue} 68% 55%)`;
}

/** Hover text: `10B → D major`. Null when not a Camelot key. */
export function camelotKeyHoverLabel(key: string | null): string | null {
	if (key === null) return null;
	const parts = _parseCamelotParts(key);
	if (parts === null) return null;
	const root = CAMELOT_ROOTS[parts.mode][parts.n - 1];
	const note = NOTE_NAMES[root];
	const quality = parts.mode === 'B' ? 'major' : 'minor';
	const camelot = `${parts.n}${parts.mode}`;
	return `${camelot} → ${note} ${quality}`;
}
