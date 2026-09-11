/**
 * Browser search query grammar (pin 7ca47b21ead7, issue #936):
 * `field:operator:value` predicates over BPM, rating and key, layered on
 * top of the browser's existing plain substring search and `genre:` /
 * `genre:~` tag filters (pane-contract.svelte.ts's filterRows).
 *
 * Pure, dependency-free module - no BrowserRow import, no DOM - so every
 * edge case (malformed input, unknown fields, multi-term AND) is pinned in
 * isolation from the table that consumes it. filterRows adapts a BrowserRow
 * into SearchableTrack (resolving the genre rb_meta fallback itself) and
 * calls matchesSearchQuery per row.
 *
 * Grammar, in order of precedence:
 * - A query that is ENTIRELY `genre:<tag>` or `genre:~<tag>` (the whole
 *   trimmed string, spaces and all) is the pre-existing genre chip path -
 *   preserved byte-for-byte so a multi-word tag (`genre:Deep House`) still
 *   works. This is checked BEFORE tokenizing.
 * - Otherwise the query is split on whitespace into terms, each either a
 *   recognized field predicate (`bpm:`, `rating:`, `key:`, `genre:`) or a
 *   plain substring term. All terms AND together.
 * - If NO term parses as a field predicate, the plain-substring path is
 *   applied to the WHOLE raw string (not the rejoined tokens), which is
 *   exactly today's pre-pin behaviour - unprefixed search is untouched.
 * - An unrecognized field name, or a recognized field with a value that
 *   fails to parse, degrades that one term to a plain substring rather
 *   than matching nothing (a typo must never silently empty the table).
 */

export interface SearchableTrack {
	title: string | null;
	artist: string | null;
	comments: string | null;
	key: string | null;
	/** Caller resolves any rb_meta fallback before calling - this module
	 * only sees the one string it should search. */
	genre: string | null;
	bpm: number | null;
	rating: number | null;
}

/** Fallback fields for a plain (unprefixed) substring term - identical set
 * pre-pin filterRows always searched. */
const _PLAIN_FIELDS = ['title', 'artist', 'comments', 'key', 'genre'] as const;

type NumericField = 'bpm' | 'rating';
type NumericOp = 'eq' | 'gte' | 'lte' | 'gt' | 'lt' | 'range';

interface NumericPredicate {
	kind: 'numeric';
	field: NumericField;
	op: NumericOp;
	value: number;
	valueMax?: number;
}

interface KeyPredicate {
	kind: 'key';
	value: string;
}

interface GenrePredicate {
	kind: 'genre';
	loose: boolean;
	tag: string;
}

interface PlainTerm {
	kind: 'plain';
	text: string;
}

type Term = NumericPredicate | KeyPredicate | GenrePredicate | PlainTerm;

const _WHOLE_GENRE_STRICT = /^genre:(?!~)(.+)$/i;
const _WHOLE_GENRE_LOOSE = /^genre:~(.+)$/i;
const _NUMERIC_FIELD_TOKEN = /^(bpm|rating):(.*)$/i;
const _KEY_FIELD_TOKEN = /^key:(.*)$/i;
const _GENRE_FIELD_TOKEN = /^genre:(~)?(.*)$/i;
const _RANGE_VALUE = /^(-?\d+(?:\.\d+)?)-(-?\d+(?:\.\d+)?)$/;
const _COMPARATOR_VALUE = /^(>=|<=|>|<|=)?(-?\d+(?:\.\d+)?)$/;

function _parseNumericValue(field: NumericField, raw: string): NumericPredicate | null {
	const range = _RANGE_VALUE.exec(raw);
	if (range !== null) {
		const lo = Number(range[1]);
		const hi = Number(range[2]);
		if (lo > hi) return null; // backwards range - degrade to substring
		return { kind: 'numeric', field, op: 'range', value: lo, valueMax: hi };
	}
	const cmp = _COMPARATOR_VALUE.exec(raw);
	if (cmp !== null) {
		const op: NumericOp =
			cmp[1] === '>=' ? 'gte' : cmp[1] === '<=' ? 'lte' : cmp[1] === '>' ? 'gt' : cmp[1] === '<' ? 'lt' : 'eq';
		return { kind: 'numeric', field, op, value: Number(cmp[2]) };
	}
	return null;
}

/** One search term, parsed to a structured predicate or left as a plain
 * substring term (unrecognized field name or unparseable value). */
function _parseTerm(token: string): Term {
	const genre = _GENRE_FIELD_TOKEN.exec(token);
	if (genre !== null) {
		const tag = genre[2].trim().toLowerCase();
		if (tag !== '') return { kind: 'genre', loose: genre[1] !== undefined, tag };
		return { kind: 'plain', text: token };
	}
	const key = _KEY_FIELD_TOKEN.exec(token);
	if (key !== null) {
		const value = key[1].trim().toLowerCase();
		if (value !== '') return { kind: 'key', value };
		return { kind: 'plain', text: token };
	}
	const numeric = _NUMERIC_FIELD_TOKEN.exec(token);
	if (numeric !== null) {
		const field = numeric[1].toLowerCase() as NumericField;
		const parsed = _parseNumericValue(field, numeric[2].trim());
		if (parsed !== null) return parsed;
		return { kind: 'plain', text: token };
	}
	return { kind: 'plain', text: token };
}

function _genreTokens(track: SearchableTrack): string[] {
	const raw = track.genre ?? '';
	if (raw.trim() === '') return [];
	return raw
		.split(',')
		.map((t) => t.trim().toLowerCase())
		.filter((t) => t !== '');
}

function _matchesPlain(track: SearchableTrack, text: string): boolean {
	const q = text.toLowerCase();
	if (q === '') return true;
	return _PLAIN_FIELDS.some((f) => {
		const value = track[f];
		return value !== null && value.toLowerCase().includes(q);
	});
}

function _matchesNumeric(track: SearchableTrack, p: NumericPredicate): boolean {
	const raw = track[p.field];
	if (raw === null) return false;
	if (p.op === 'range') return raw >= p.value && raw <= (p.valueMax as number);
	else if (p.op === 'gte') return raw >= p.value;
	else if (p.op === 'lte') return raw <= p.value;
	else if (p.op === 'gt') return raw > p.value;
	else if (p.op === 'lt') return raw < p.value;
	else return raw === p.value;
}

function _matchesKey(track: SearchableTrack, p: KeyPredicate): boolean {
	const raw = track.key;
	return raw !== null && raw.toLowerCase().includes(p.value);
}

function _matchesGenre(track: SearchableTrack, p: GenrePredicate): boolean {
	if (p.loose) return (track.genre ?? '').toLowerCase().includes(p.tag);
	return _genreTokens(track).some((t) => t === p.tag);
}

function _matchesTerm(track: SearchableTrack, term: Term): boolean {
	if (term.kind === 'numeric') return _matchesNumeric(track, term);
	else if (term.kind === 'key') return _matchesKey(track, term);
	else if (term.kind === 'genre') return _matchesGenre(track, term);
	else return _matchesPlain(track, term.text);
}

/** True iff `track` matches `query` under the grammar above. Case-insensitive
 * throughout; an empty/whitespace-only query matches everything. */
export function matchesSearchQuery(track: SearchableTrack, query: string): boolean {
	const raw = query.trim();
	if (raw === '') return true;

	// Whole-string genre chip path, byte-for-byte the pre-pin behaviour -
	// preserves a multi-word tag as one predicate.
	const strictWhole = _WHOLE_GENRE_STRICT.exec(raw);
	if (strictWhole !== null) {
		const tag = strictWhole[1].trim().toLowerCase();
		return tag === '' ? true : _genreTokens(track).some((t) => t === tag);
	}
	const looseWhole = _WHOLE_GENRE_LOOSE.exec(raw);
	if (looseWhole !== null) {
		const tag = looseWhole[1].trim().toLowerCase();
		return tag === '' ? true : (track.genre ?? '').toLowerCase().includes(tag);
	}

	const terms = raw.split(/\s+/).map(_parseTerm);
	const hasPredicate = terms.some((t) => t.kind !== 'plain');
	if (!hasPredicate) return _matchesPlain(track, raw);

	return terms.every((t) => _matchesTerm(track, t));
}

/** Explainer copy (hover panel + empty-state, SearchBox.svelte) - each
 * example is asserted (browser-search-query.test.mjs) to actually parse as
 * the predicate it claims, so the taught syntax can never drift from the
 * grammar above. */
export const SEARCH_QUERY_HELP: ReadonlyArray<{ example: string; hint: string }> = [
	{ example: 'bpm:120-128', hint: 'BPM range (inclusive)' },
	{ example: 'bpm:>=120', hint: 'BPM at or above - also <=, >, <, =' },
	{ example: 'rating:>=4', hint: 'Star rating at or above (1-5)' },
	{ example: 'key:8A', hint: 'Camelot key' },
	{ example: 'genre:house', hint: 'Genre, exact tag match' },
	{ example: 'genre:~house', hint: 'Genre, loose substring match' }
];
