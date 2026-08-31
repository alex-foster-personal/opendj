/**
 * Camelot key swatch colour + hover label for the browser table and deck
 * header.
 *
 * Parsing is delegated to player/key/camelot.ts, the single key parser the
 * engine, KEY SYNC, AutoPlay and next-only all share. This file used to carry
 * its OWN regex and its own copy of the root tables, which is why a track
 * tagged in musical notation ("Gm") rendered with no colour and no tooltip
 * while the same key worked elsewhere. One parser, one answer.
 */
import { parseCamelotKey } from '$lib/player/key/camelot';

/** Classic Camelot wheel hues (1..12); A/B share the number colour. */
const CAMELOT_HUES = [0, 30, 55, 85, 120, 155, 185, 210, 240, 275, 300, 330] as const;

/** Pitch-class → display name (C=0). Mixed sharp/flat labels match common Camelot charts. */
const NOTE_NAMES = ['C', 'C#', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B'] as const;

/** Colour for a track key, or null when the string is in no notation this
 * app parses. Musical notation resolves to its Camelot wheel colour, so "Gm"
 * and "6A" are the same swatch. */
export function camelotKeyColor(key: string | null): string | null {
	const parsed = parseCamelotKey(key);
	if (parsed === null) return null;
	const hue = CAMELOT_HUES[parsed.number - 1];
	const major = parsed.mode === 'B';
	return major ? `hsl(${hue} 72% 62%)` : `hsl(${hue} 68% 55%)`;
}

/** Hover text: `10B → D major`. Musical notation is normalized to Camelot
 * first, so "Gm" reads `6A → G minor`. Null when the key does not parse. */
export function camelotKeyHoverLabel(key: string | null): string | null {
	const parsed = parseCamelotKey(key);
	if (parsed === null) return null;
	const note = NOTE_NAMES[parsed.root];
	const quality = parsed.mode === 'B' ? 'major' : 'minor';
	return `${parsed.number}${parsed.mode} → ${note} ${quality}`;
}
