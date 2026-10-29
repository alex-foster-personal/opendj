/**
 * One key notation for every key cell (operator report, Sat 24 Oct 2026).
 *
 * The library used to print whatever string a source had stored, so one table
 * mixed rekordbox's Camelot "8A" with its spelled "Am" and "F#m". The server
 * now serves every stored key as canonical Camelot
 * (apps/analysis_key/canon.py `canonical_key_text`); this module renders that
 * key in the ONE notation the user picked (`ui-prefs.key_notation`).
 *
 * Parsing is delegated to player/key/camelot.ts, the single browser-side key
 * parser, so a raw value that reaches the browser by another path still lands
 * in the same notation. A key that parses in no notation is returned as-is
 * with `parsed: false`, so the cell can flag it rather than guess.
 */
import { parseCamelotKey, type CamelotKey } from '$lib/player/key/camelot';

export type KeyNotation = 'camelot_musical' | 'camelot' | 'open_key' | 'musical';

/** The first entry is the default (the maintainer, Tue 27 Oct 2026): Camelot and musical
 * side by side, `8A • Am`, so neither half has to be translated in the head. */
export const KEY_NOTATIONS: readonly KeyNotation[] = ['camelot_musical', 'camelot', 'open_key', 'musical'];

export const DEFAULT_KEY_NOTATION: KeyNotation = 'camelot_musical';

/** Separator between the Camelot and musical halves: space, U+2022, space. */
export const CAMELOT_MUSICAL_SEPARATOR = ' \u2022 ';

export function isKeyNotation(value: unknown): value is KeyNotation {
	return typeof value === 'string' && (KEY_NOTATIONS as readonly string[]).includes(value);
}

/** Pitch-class spelling (C = 0), the same one the server writes rekordbox
 * scale names with (canon.py `_PITCH_SPELLING`): flats except F#. */
const PITCH_SPELLING = ['C', 'Db', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B'] as const;

export interface KeyDisplay {
	/** What the cell prints. */
	text: string;
	/** False when the stored key matched no notation; `text` is then the raw value. */
	parsed: boolean;
}

/** Open Key number = Camelot number + 5, mod 12, 1-indexed (canon.py docstring). */
function _openKey(key: CamelotKey): string {
	const number = ((key.number + 4) % 12) + 1;
	return `${number}${key.mode === 'A' ? 'm' : 'd'}`;
}

function _musical(key: CamelotKey): string {
	const name = PITCH_SPELLING[key.root];
	return key.mode === 'A' ? `${name}m` : name;
}

/** A stored key, or a `camelot_musical` string this module rendered: the
 * Camelot half decides, and the musical half must name the same key. */
function _parseAnyKey(raw: string): CamelotKey | null {
	const bullet = raw.indexOf('\u2022');
	if (bullet === -1) return parseCamelotKey(raw);
	const camelot = parseCamelotKey(raw.slice(0, bullet));
	const musical = parseCamelotKey(raw.slice(bullet + 1));
	if (camelot === null || musical === null) return null;
	if (camelot.number !== musical.number || camelot.mode !== musical.mode) return null;
	return camelot;
}

function renderKey(key: CamelotKey, notation: KeyNotation): string {
	switch (notation) {
		case 'camelot_musical':
			return `${key.number}${key.mode}${CAMELOT_MUSICAL_SEPARATOR}${_musical(key)}`;
		case 'camelot':
			return `${key.number}${key.mode}`;
		case 'open_key':
			return _openKey(key);
		case 'musical':
			return _musical(key);
		default: {
			const exhaustive: never = notation;
			throw new Error(`Unhandled key notation: ${String(exhaustive)}`);
		}
	}
}

/** A stored key in every notation, for search; the raw text alone when it
 * parses in none, and nothing when there is no key. */
export function keyForms(raw: string | null): string[] {
	if (raw === null || raw.trim() === '') return [];
	const parsed = _parseAnyKey(raw);
	if (parsed === null) return [raw];
	return KEY_NOTATIONS.map((notation) => renderKey(parsed, notation));
}

/** The cell text for a stored key, or null when there is no key at all. */
export function displayKey(raw: string | null, notation: KeyNotation): KeyDisplay | null {
	if (raw === null || raw.trim() === '') return null;
	const parsed = _parseAnyKey(raw);
	if (parsed === null) return { text: raw, parsed: false };
	return { text: renderKey(parsed, notation), parsed: true };
}

/** The label a notation choice shows in a menu (LIBUX-54 column chooser):
 * the sample key 8A in that notation, except Open Key, which is named rather
 * than sampled, because "1m" alone does not say which notation it is
 * (sol review P1, PR #7601). */
export function keyNotationChoiceLabel(notation: KeyNotation): string {
	if (notation === 'open_key') return 'Open Key';
	return renderKey(_sampleKey(), notation);
}

function _sampleKey(): CamelotKey {
	const sample = parseCamelotKey('8A');
	if (sample === null) throw new Error('key-notation: the sample key 8A did not parse');
	return sample;
}

/** The text a non-table surface (deck header, inspector, preview bar) prints:
 * the key in `notation`, the stored text when it parses in none, and null when
 * there is no key. */
export function formatKey(raw: string | null | undefined, notation: KeyNotation): string | null {
	return displayKey(raw ?? null, notation)?.text ?? null;
}

/** Every key string a column can print in `notation` (one per Camelot key),
 * for sizing a column before any row is loaded. */
export function allKeyTexts(notation: KeyNotation): string[] {
	const texts: string[] = [];
	for (let number = 1; number <= 12; number += 1) {
		for (const mode of ['A', 'B'] as const) {
			const shown = displayKey(`${number}${mode}`, notation);
			if (shown !== null) texts.push(shown.text);
		}
	}
	return texts;
}

/** Where the current notation is read from. prefs.svelte.ts installs
 * `() => uiPrefs.key_notation` when it loads, so this module needs no prefs
 * import and every surface reads the one setting. */
let _notationSource: (() => KeyNotation) | null = null;

export function installKeyNotationSource(source: () => KeyNotation): void {
	_notationSource = source;
}

/** The key in the user's notation (ui-prefs `key_notation`): what the deck
 * header, inspector and every other key display prints. Reactive in a
 * component, since the installed source reads `$state`. */
export function shownKey(raw: string | null | undefined): string | null {
	if (_notationSource === null) {
		throw new Error('shownKey: no key notation source; $lib/rb/prefs.svelte installs it on load');
	}
	return formatKey(raw, _notationSource());
}

/** The notation every surface is printing in right now (the installed source). */
export function shownKeyNotation(): KeyNotation {
	if (_notationSource === null) {
		throw new Error('shownKeyNotation: no key notation source; $lib/rb/prefs.svelte installs it on load');
	}
	return _notationSource();
}
