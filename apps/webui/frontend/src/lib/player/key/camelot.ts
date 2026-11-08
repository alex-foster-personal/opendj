/**
 * Camelot key algebra and KEY SYNC derivation. Pure: no runtime, no stores, no
 * audio nodes. Extracted verbatim from audio-engine.svelte.ts (T4 S1).
 *
 * DESIGN DRIFT: `masterTempoSemitones` lands here, not in key/key-shift.ts as
 * the T4 module table assigns it. `composeStretchSemitones` calls it, and the
 * design's own diagram points key-shift -> camelot, so the assigned split is a
 * cycle. Arithmetic unchanged.
 */

import { CAMELOT_RULES, type CamelotRule, type CamelotRuleId } from './camelot-rules.generated';
import { DECK_IDS } from '$lib/player/constants';
import type { DeckId } from '$lib/rb/deck-id';

export function masterTempoSemitones(tempoRatio: number, enabled: boolean): number {
	if (!Number.isFinite(tempoRatio) || tempoRatio <= 0) {
		throw new RangeError(`tempo ratio must be a finite positive number, got ${tempoRatio}`);
	}
	return enabled ? 0 : 12 * Math.log2(tempoRatio);
}

/** Parsed, canonical Camelot key. `root` is a chromatic pitch class where C
 * is 0. Numbered Camelot notation ("8A") and standard musical notation
 * ("Gm", "F#", "Ab major") both parse. Musical notation is TABLE-DRIVEN, never
 * guessed: a recognized spelling maps to exactly one pitch class and one
 * wheel, and an unrecognized one returns null so the caller still fails at its
 * own operation boundary. */
export interface CamelotKey {
	number: number;
	mode: 'A' | 'B';
	root: number;
}

const CAMELOT_ROOTS: Record<CamelotKey['mode'], readonly number[]> = {
	// 1A = Ab minor through 12A = C# minor.
	A: [8, 3, 10, 5, 0, 7, 2, 9, 4, 11, 6, 1],
	// 1B = B major through 12B = E major.
	B: [11, 6, 1, 8, 3, 10, 5, 0, 7, 2, 9, 4]
};

function _pitchClass(semitones: number): number {
	return ((semitones % 12) + 12) % 12;
}

/** Note letter -> chromatic pitch class, C = 0. */
const NOTE_PITCH_CLASSES: Readonly<Record<string, number>> = {
	C: 0,
	D: 2,
	E: 4,
	F: 5,
	G: 7,
	A: 9,
	B: 11
};

/** Accidental glyph -> semitone offset. ASCII and Unicode spellings both turn
 * up in real rekordbox / Mixed In Key / Serato metadata. */
const ACCIDENTAL_OFFSETS: Readonly<Record<string, number>> = {
	'': 0,
	'#': 1,
	'\u266f': 1,
	b: -1,
	'\u266d': -1
};

/**
 * Mode spelling -> Camelot wheel (A = minor, B = major).
 *
 * Case is significant for the one-letter forms and only for those: bare `m` is
 * minor, bare `M` is major, the lead-sheet convention every vendor writing
 * one-letter modes follows. Word forms are uppercased before lookup because
 * "Minor"/"minor"/"MINOR" carry no competing meaning.
 *
 * An empty mode resolves to major. That is notation rather than a hidden
 * default: "F#" denotes F# major in every source this metadata arrives from.
 */
const ONE_LETTER_MODES: Readonly<Record<string, CamelotKey['mode']>> = {
	'': 'B',
	m: 'A',
	M: 'B'
};
const WORD_MODES: Readonly<Record<string, CamelotKey['mode']>> = {
	MIN: 'A',
	MINOR: 'A',
	MAJ: 'B',
	MAJOR: 'B'
};

/** `8A` / `12b`. Camelot notation always leads with the wheel number, so it
 * can never collide with the note-letter grammar below. */
const _CAMELOT_RE = /^(1[0-2]|[1-9])([ab])$/i;
/** `1m` / `12d`: Open Key (Traktor, Mixed In Key). `m` is minor and `d` is
 * major; the number is the Camelot number + 5, mod 12, 1-indexed. Lower case
 * only: an upper-case `M`/`D` is not Open Key in any source this app reads. */
const _OPEN_KEY_RE = /^(1[0-2]|[1-9])([dm])$/;
/** `Gm`, `F#`, `Bbm`, `Ab major`, `C# minor`. The only separator allowed
 * between the note and a spelled-out mode is whitespace. */
const _MUSICAL_RE = /^([A-Ga-g])([#b\u266f\u266d]?)\s*([A-Za-z]*)$/;

/** Wheel position for a pitch class. Each wheel lists all 12 pitch classes
 * exactly once, so this is total; a miss means CAMELOT_ROOTS itself was edited
 * into an invalid state, which must be loud rather than quietly yield a wrong
 * harmonic relationship. */
function _camelotFromRoot(root: number, mode: CamelotKey['mode']): CamelotKey {
	const number = CAMELOT_ROOTS[mode].indexOf(root) + 1;
	if (number === 0) {
		throw new Error(`Camelot ${mode} wheel has no entry for pitch class ${root}`);
	}
	return { number, mode, root };
}

/** Resolve a musical-notation mode token, or null when the token is not a
 * spelling these tables know. Never guesses - an unknown token rejects the
 * whole key. */
function _modeFromToken(token: string): CamelotKey['mode'] | null {
	if (token.length <= 1) return ONE_LETTER_MODES[token] ?? null;
	return WORD_MODES[token.toUpperCase()] ?? null;
}

/**
 * Parse a track key into canonical Camelot form.
 *
 * Accepts numbered Camelot ("8A", "12b"), Open Key ("1m" -> 8A, "12d" -> 7B)
 * and standard musical notation ("Gm" -> 6A, "F#" -> 2B, "Ab major" -> 4B,
 * "Bbm" -> 3A). Returns null for
 * metadata in neither notation, so callers can fail explicitly at their own
 * operation boundary rather than inventing a harmonic relationship.
 */
export function parseCamelotKey(value: string | null): CamelotKey | null {
	if (typeof value !== 'string') return null;
	const text = value.trim();

	const camelot = _CAMELOT_RE.exec(text);
	if (camelot !== null) {
		const number = Number(camelot[1]);
		const mode = camelot[2].toUpperCase() as CamelotKey['mode'];
		return { number, mode, root: CAMELOT_ROOTS[mode][number - 1] };
	}

	const openKey = _OPEN_KEY_RE.exec(text);
	if (openKey !== null) {
		const number = ((Number(openKey[1]) + 6) % 12) + 1;
		const mode: CamelotKey['mode'] = openKey[2] === 'm' ? 'A' : 'B';
		return { number, mode, root: CAMELOT_ROOTS[mode][number - 1] };
	}

	const musical = _MUSICAL_RE.exec(text);
	if (musical === null) return null;
	const mode = _modeFromToken(musical[3]);
	if (mode === null) return null;
	const base = NOTE_PITCH_CLASSES[musical[1].toUpperCase()];
	const accidental = ACCIDENTAL_OFFSETS[musical[2]];
	if (base === undefined || accidental === undefined) return null;
	return _camelotFromRoot(_pitchClass(base + accidental), mode);
}

/** Exported (name kept) for the call sites that used it while it was file-scoped. */
export function _assertKeyShift(semitones: number): asserts semitones is number {
	if (!Number.isInteger(semitones)) {
		throw new TypeError(`key shift must be an integer number of semitones, got ${semitones}`);
	}
	if (semitones < -12 || semitones > 12) {
		throw new RangeError(`key shift must be within -12..12 semitones, got ${semitones}`);
	}
}

/** Validate a DSP pitch sample during a live key-shift ramp. Public key-shift
 * commands remain integer-only through `_assertKeyShift`; Signalsmith accepts
 * the fractional intermediates that join those command values. */
export function assertKeyShiftPitch(semitones: number): asserts semitones is number {
	if (!Number.isFinite(semitones)) {
		throw new TypeError(`key-shift pitch must be finite, got ${semitones}`);
	}
	if (semitones < -12 || semitones > 12) {
		throw new RangeError(`key-shift pitch must be within -12..12 semitones, got ${semitones}`);
	}
}

function _shiftCamelotKey(key: CamelotKey, semitones: number): CamelotKey {
	_assertKeyShift(semitones);
	const root = _pitchClass(key.root + semitones);
	const number = CAMELOT_ROOTS[key.mode].indexOf(root) + 1;
	if (number === 0) throw new Error(`Camelot ${key.mode} root ${root} cannot be represented`);
	return { number, mode: key.mode, root };
}

/** Audible Camelot label after an integer manual key shift (mode preserved). */
export function effectiveCamelotKey(key: string | null, semitones: number): string | null {
	const parsed = parseCamelotKey(key);
	if (parsed === null) return key;
	if (semitones === 0) return `${parsed.number}${parsed.mode}`;
	_assertKeyShift(semitones);
	const shifted = _shiftCamelotKey(parsed, semitones);
	return `${shifted.number}${shifted.mode}`;
}

/** How far one key-nudge click moves.
 *
 * `semitone` is the standard DJ key shift (CDJ, rekordbox, Serato, Traktor):
 * one chromatic step. `tone` is a whole step. `scale` is the next degree of
 * this track's scale (natural minor on the A wheel, major on the B wheel),
 * measured from the current shift, so the size is 1 or 2 and an off-scale
 * shift snaps back onto the scale. `camelot` is one same-wheel step, a
 * perfect fifth (+7 / -7), which moves the Camelot number by one.
 * Scale returns null when the key cannot be parsed. */
export type KeyNudgeStep = 'semitone' | 'tone' | 'scale' | 'camelot';

/** Every step size, in the order the deck header shows them. */
export const KEY_NUDGE_STEPS: readonly KeyNudgeStep[] = ['semitone', 'tone', 'scale', 'camelot'];

export function isKeyNudgeStep(value: unknown): value is KeyNudgeStep {
	return typeof value === 'string' && (KEY_NUDGE_STEPS as readonly string[]).includes(value);
}

// DECKGAP-7691b: the one step size every deck's arrows use. Semitone is the
// standard DJ key shift. Set only through the `key_nudge_step` performance
// command; read by the deck header and by query state.
let _keyNudgeStep: KeyNudgeStep = 'semitone';
const _keyNudgeStepListeners = new Set<(step: KeyNudgeStep) => void>();

export function readKeyNudgeStep(): KeyNudgeStep {
	return _keyNudgeStep;
}

export function writeKeyNudgeStep(step: KeyNudgeStep): void {
	if (!isKeyNudgeStep(step)) throw new RangeError(`key nudge step must be one of ${KEY_NUDGE_STEPS.join(', ')}, got ${String(step)}`);
	_keyNudgeStep = step;
	for (const fn of _keyNudgeStepListeners) fn(step);
}

export function subscribeKeyNudgeStep(fn: (step: KeyNudgeStep) => void): () => void {
	_keyNudgeStepListeners.add(fn);
	return () => _keyNudgeStepListeners.delete(fn);
}

const SCALE_INTERVALS: Record<CamelotKey['mode'], readonly number[]> = {
	A: [0, 2, 3, 5, 7, 8, 10],
	B: [0, 2, 4, 5, 7, 9, 11]
};

function _scaleStepSemitones(key: CamelotKey, currentShift: number, direction: -1 | 1): number {
	const scale = SCALE_INTERVALS[key.mode];
	const position = _pitchClass(currentShift);
	if (direction === 1) {
		for (const interval of scale) {
			if (interval > position) return interval - position;
		}
		return 12 - position;
	}
	for (let i = scale.length - 1; i >= 0; i--) {
		const interval = scale[i];
		if (interval < position) return interval - position;
	}
	return scale[scale.length - 1] - 12 - position;
}

export function keyNudgeSemitones(
	step: KeyNudgeStep,
	direction: -1 | 1,
	key: string | null,
	currentShift = 0
): number | null {
	if (direction !== -1 && direction !== 1) {
		throw new RangeError(`key nudge direction must be -1 or 1, got ${String(direction)}`);
	}
	if (step === 'semitone') return direction;
	if (step === 'tone') return direction * 2;
	if (step === 'camelot') return direction * 7;
	if (step !== 'scale') {
		throw new RangeError(`unknown key nudge step ${String(step)}`);
	}
	const parsed = parseCamelotKey(key);
	if (parsed === null) return null;
	if (!Number.isInteger(currentShift)) {
		throw new TypeError(`key nudge shift must be an integer, got ${String(currentShift)}`);
	}
	return _scaleStepSemitones(parsed, currentShift, direction);
}

function _camelotCircularDistance(left: number, right: number): number {
	const raw = Math.abs(left - right);
	return Math.min(raw, 12 - raw);
}

/** AlphaTheta/Pioneer least-change families: same-wheel and cross-wheel keys
 * are compatible at the same Camelot number and one step either direction.
 * That makes the six named relationships (A/A and A/B, each same/+1/-1)
 * symmetric and preserves 1 <-> 12 wraparound. */
export function camelotKeysWithinSteps(
	deckKey: string | null,
	masterKey: string | null,
	maxSteps: number
): boolean {
	const deck = parseCamelotKey(deckKey);
	const master = parseCamelotKey(masterKey);
	if (deck === null || master === null) return false;
	return _camelotCircularDistance(deck.number, master.number) <= maxSteps;
}

export function camelotKeysAreCompatible(
	deckKey: string | null,
	masterKey: string | null
): boolean {
	return camelotKeysWithinSteps(deckKey, masterKey, 1);
}

/** Compatible-filter key rule (KEYSHIFT-01). `transition` is the enabled
 * Camelot rule checkboxes (camelotRulesAllow); `mashup` is the exact same key,
 * plus the relative major/minor at the same number when `relative`. A
 * transition rule with no ids never reaches here: the caller treats it as
 * "key not checked". */
export type CompatibleKeyRule =
	| { mode: 'transition'; rules: readonly CamelotRuleId[] }
	| { mode: 'mashup'; relative: boolean };

/** A row's key match against the reference after an integer key shift. */
export interface CompatibleKeyShift {
	/** Semitones the row is shifted by to match; 0 = compatible as it is. */
	shift: number;
	/** The row's key after that shift, in Camelot notation ("8A"). */
	key: string;
}

/** Largest allowed key shift for the compatible filter (KEYSHIFT-03). */
export const COMPATIBLE_KEY_SHIFT_MAX = 3;

function _keysMatchRule(row: CamelotKey, ref: CamelotKey, rule: CompatibleKeyRule): boolean {
	if (rule.mode === 'mashup') {
		if (row.number !== ref.number) return false;
		return row.mode === ref.mode || rule.relative;
	}
	return camelotRulesAllow(`${ref.number}${ref.mode}`, `${row.number}${row.mode}`, rule.rules);
}

/** Shift order for a search up to `maxShift`: 0 first, then the smallest
 * magnitude, and up before down at equal magnitude. Up first because upward
 * shifts measured cleaner on our stretcher (ops/quality/stretch/report.md:
 * +2 semitones 2.97 dB LSD, -2 semitones 3.56 dB). */
export function compatibleShiftOrder(maxShift: number): number[] {
	if (!Number.isInteger(maxShift) || maxShift < 0 || maxShift > COMPATIBLE_KEY_SHIFT_MAX) {
		throw new RangeError(`key shift max must be an integer 0..${COMPATIBLE_KEY_SHIFT_MAX}, got ${maxShift}`);
	}
	const order = [0];
	for (let m = 1; m <= maxShift; m += 1) order.push(m, -m);
	return order;
}

/**
 * The smallest key shift (|shift| <= `maxShift`) that makes `rowKey` compatible
 * with `refKey` under `rule`, or null when none does or either key does not
 * parse. An unshifted match always wins (KEYSHIFT-02).
 */
export function compatibleKeyShift(
	rowKey: string | null,
	refKey: string | null,
	rule: CompatibleKeyRule,
	maxShift: number
): CompatibleKeyShift | null {
	const order = compatibleShiftOrder(maxShift);
	const row = parseCamelotKey(rowKey);
	const ref = parseCamelotKey(refKey);
	if (row === null || ref === null) return null;
	for (const shift of order) {
		const shifted = shift === 0 ? row : _shiftCamelotKey(row, shift);
		if (_keysMatchRule(shifted, ref, rule)) {
			return { shift, key: `${shifted.number}${shifted.mode}` };
		}
	}
	return null;
}

function _assertEffectiveAudibleSemitones(name: string, value: number): void {
	if (!Number.isFinite(value)) {
		throw new RangeError(`${name} effective audible semitones must be finite, got ${value}`);
	}
}

function _circularPitchDistance(left: number, right: number): number {
	const distance = Math.abs(_pitchClass(left - right));
	return Math.min(distance, 12 - distance);
}

function _keySyncNudgeCandidates(): number[] {
	const candidates: number[] = [];
	for (let magnitude = 0; magnitude <= 12; magnitude += 1) {
		if (magnitude === 0) candidates.push(0);
		else candidates.push(-magnitude, magnitude);
	}
	return candidates;
}

/** Pick the smallest integer manual nudge whose audible pitch is closest to
 * one of the six Pioneer-compatible Camelot family roots. The source deck
 * mode is retained, while each deck and master may already have a fractional
 * Signalsmith offset from Master Tempo-off tempo compensation. */
export function deriveKeySyncNudge(
	deckKey: string | null,
	masterKey: string | null,
	deckEffectiveAudibleSemitones: number,
	masterEffectiveAudibleSemitones: number,
	deckManualShiftSemitones: number
): number {
	const deck = parseCamelotKey(deckKey);
	const master = parseCamelotKey(masterKey);
	if (deck === null) throw new Error('KEY SYNC requires a parseable Camelot key on the deck');
	if (master === null) throw new Error('KEY SYNC requires a parseable Camelot key on the master');
	_assertEffectiveAudibleSemitones('deck', deckEffectiveAudibleSemitones);
	_assertEffectiveAudibleSemitones('master', masterEffectiveAudibleSemitones);
	_assertKeyShift(deckManualShiftSemitones);

	let bestNudge: number | null = null;
	let bestDistance = Number.POSITIVE_INFINITY;
	for (const nudge of _keySyncNudgeCandidates()) {
		const nextManualShift = deckManualShiftSemitones + nudge;
		if (nextManualShift < -12 || nextManualShift > 12) continue;
		const deckAudibleRoot = deck.root + deckEffectiveAudibleSemitones + nudge;
		for (let number = 1; number <= 12; number += 1) {
			if (_camelotCircularDistance(number, master.number) > 1) continue;
			const familyRoot = CAMELOT_ROOTS[deck.mode][number - 1] + masterEffectiveAudibleSemitones;
			const distance = _circularPitchDistance(deckAudibleRoot, familyRoot);
			if (distance < bestDistance - 1e-12) {
				bestDistance = distance;
				bestNudge = nudge;
			}
		}
	}
	if (bestNudge === null) {
		throw new RangeError('KEY SYNC cannot apply a compatible nudge within -12..12 manual semitones');
	}
	return bestNudge;
}

//----------------------------------------------------------------- KEY SYNC modes (KEYSYNC-01)
// exact (default, the maintainer Wed 4 Nov 2026): transpose onto the leader's key, as
// Serato Pitch 'n Time Key Sync does. relative: land on the leader's Camelot
// number in this deck's own mode (A<->B relative major/minor, the same notes).
// compatible: the Pioneer / rekordbox family-root behaviour (deriveKeySyncNudge).

export type KeySyncMode = 'exact' | 'relative' | 'compatible';
export const KEY_SYNC_MODES: readonly KeySyncMode[] = ['exact', 'relative', 'compatible'];

export function isKeySyncMode(value: unknown): value is KeySyncMode {
	return typeof value === 'string' && (KEY_SYNC_MODES as readonly string[]).includes(value);
}

/** Shortest signed path in semitones, -5..+6: the tritone tie is +6, the
 * same rule as Mixxx's KeyUtils shortest steps (and up measured cleaner on
 * our stretcher, ops/quality/stretch/report.md). Fractional input rounds. */
function _shortestSignedSemitones(semitones: number): number {
	const folded = ((semitones % 12) + 12) % 12;
	return Math.round(folded > 6 ? folded - 12 : folded);
}

function _keySyncTargetRoot(deck: CamelotKey, leader: CamelotKey, mode: 'exact' | 'relative'): number {
	if (mode === 'exact' && deck.mode !== leader.mode) {
		throw new RangeError(
			`KEY SYNC exact cannot transpose ${deck.number}${deck.mode} onto ${leader.number}${leader.mode}: the modes differ; use relative to land on ${leader.number}${deck.mode}`
		);
	}
	return CAMELOT_ROOTS[deck.mode][leader.number - 1];
}

function _parseKeySyncKeys(deckKey: string | null, leaderKey: string | null): [CamelotKey, CamelotKey] {
	const deck = parseCamelotKey(deckKey);
	const leader = parseCamelotKey(leaderKey);
	if (deck === null) throw new Error('KEY SYNC requires a parseable Camelot key on the deck');
	if (leader === null) throw new Error('KEY SYNC requires a parseable Camelot key on the leader');
	return [deck, leader];
}

/** Semitones that move `followerKey` onto `leaderKey` under `mode`, -6..+6. */
export function keySyncSemitoneDelta(
	followerKey: string | null,
	leaderKey: string | null,
	mode: 'exact' | 'relative'
): number {
	if (mode !== 'exact' && mode !== 'relative') throw new RangeError(`keySyncSemitoneDelta: mode must be exact or relative, got ${String(mode)}`);
	const [deck, leader] = _parseKeySyncKeys(followerKey, leaderKey);
	return _shortestSignedSemitones(_keySyncTargetRoot(deck, leader, mode) - deck.root);
}

/** True when exact KEY SYNC cannot match: both keys parse but their modes differ. */
export function keySyncModeMismatch(followerKey: string | null, leaderKey: string | null, mode: KeySyncMode): boolean {
	if (mode !== 'exact') return false;
	const deck = parseCamelotKey(followerKey);
	const leader = parseCamelotKey(leaderKey);
	return deck !== null && leader !== null && deck.mode !== leader.mode;
}

/** DECK-KEY-01: the one key every deck surface reads (header readout, KEY SYNC,
 * ladder, nudges, tempo modal): the library's resolved key (Mixed In Key /
 * rekordbox fallback and key_source applied server side by the same resolver
 * the library row uses), else the analyser's "?" guess. */
export function keySyncKey(deck: { key: string | null; key_guess?: string | null }): string | null {
	return deck.key ?? deck.key_guess ?? null;
}

export function keySyncKeyIsGuess(deck: { key: string | null; key_guess?: string | null }): boolean {
	return deck.key === null && (deck.key_guess ?? null) !== null;
}

/** KEY-FALLBACK-01: where a shown key came from, for the library cell and the deck key hover. */
export const KEY_SOURCE_HINT: Readonly<Record<string, string>> = {
	own: 'our analysis',
	rekordbox: 'from rekordbox',
	file_tag: 'from the file tag',
	mik_tag: 'from Mixed In Key tag'
};

/** The hover phrase for a key_source, or null for none / a guess (the "?" says that). */
export function keySourceHint(source: string | null | undefined): string | null {
	return source === null || source === undefined ? null : (KEY_SOURCE_HINT[source] ?? null);
}

/** Convert a listener-facing KEY SYNC plan to the absolute manual schedule value.
 * exact/relative pick the absolute shift -6..+6 that puts this deck's audible
 * root on the leader's audible target root; the deck's own varispeed offset
 * (Master Tempo off) is the effective minus the presented manual shift. */
export function deriveKeySyncTargetManualShift(
	deckKey: string | null,
	masterKey: string | null,
	deckEffectiveAudibleSemitones: number,
	masterEffectiveAudibleSemitones: number,
	presentedManualShiftSemitones: number,
	mode: KeySyncMode
): number {
	_assertKeyShift(presentedManualShiftSemitones);
	if (mode === 'exact' || mode === 'relative') {
		_assertEffectiveAudibleSemitones('deck', deckEffectiveAudibleSemitones);
		_assertEffectiveAudibleSemitones('master', masterEffectiveAudibleSemitones);
		const [deck, leader] = _parseKeySyncKeys(deckKey, masterKey);
		const deckVarispeed = deckEffectiveAudibleSemitones - presentedManualShiftSemitones;
		const target = _shortestSignedSemitones(
			_keySyncTargetRoot(deck, leader, mode) + masterEffectiveAudibleSemitones - deck.root - deckVarispeed
		);
		_assertKeyShift(target);
		return target;
	} else if (mode !== 'compatible') {
		throw new RangeError(`KEY SYNC mode must be exact, relative or compatible, got ${String(mode)}`);
	}
	const nudge = deriveKeySyncNudge(
		deckKey,
		masterKey,
		deckEffectiveAudibleSemitones,
		masterEffectiveAudibleSemitones,
		presentedManualShiftSemitones
	);
	const target = presentedManualShiftSemitones + nudge;
	_assertKeyShift(target);
	return target;
}

/** Legacy zero-offset convenience wrapper, returning the final manual shift. */
export function deriveKeySyncSemitones(
	deckKey: string | null,
	masterKey: string | null,
	masterKeyShiftSemitones = 0
): number {
	_assertKeyShift(masterKeyShiftSemitones);
	return deriveKeySyncNudge(deckKey, masterKey, 0, masterKeyShiftSemitones, 0);
}

/** Signalsmith receives one native semitone field. Key shift composes additively
 * with the existing Master Tempo compensation, never by changing transport rate. */
export function composeStretchSemitones(
	tempoRatio: number,
	masterTempoEnabled: boolean,
	keyShiftSemitones: number
): number {
	assertKeyShiftPitch(keyShiftSemitones);
	return masterTempoSemitones(tempoRatio, masterTempoEnabled) + keyShiftSemitones;
}

/**
 * Listener-facing Camelot for KEY SYNC ladder and copy: catalog/guess key plus
 * rounded integer stretch (manual shift + Master Tempo-off tempo pitch). When
 * Master Tempo is on, tempo pitch is zero and the label matches catalog + shift.
 */
export function keySyncAudibleCamelotKey(
	key: string | null,
	tempoRatio: number,
	masterTempoEnabled: boolean,
	keyShiftSemitones: number
): string | null {
	const rounded = Math.round(composeStretchSemitones(tempoRatio, masterTempoEnabled, keyShiftSemitones));
	_assertKeyShift(rounded);
	return effectiveCamelotKey(key, rounded);
}

//----------------------------------------------------------------- KEY SYNC ladder (KEYSYNC-01)
// The chevron beside KEY SYNC lists every shift -6..+6 with the key it lands
// on and, when that key mixes harmonically with the target, the pattern name.
// Reference: the Mixed In Key Camelot harmonic-mixing guide
// (https://mixedinkey.com/harmonic-mixing-guide/): same key, relative, +-1,
// +2 energy, +7 / +5 dominant / subdominant, and the A<->B mood shift
// (minor to its parallel major is +3 numbers, B to A is -3). Steps are
// Camelot numbers from the target; `other` steps are written from an A target.

export interface KeyLadderPattern {
	label: string;
	to: 'same' | 'other';
	step: number;
}

export const KEY_LADDER_PATTERNS: readonly KeyLadderPattern[] = [
	{ label: 'exact', to: 'same', step: 0 },
	{ label: 'relative', to: 'other', step: 0 },
	{ label: '+1 energy boost', to: 'same', step: 1 },
	{ label: '-1', to: 'same', step: -1 },
	{ label: '+2 energy', to: 'same', step: 2 },
	{ label: '+7 dominant', to: 'same', step: 7 },
	{ label: '+5 subdominant', to: 'same', step: 5 },
	{ label: 'mood shift A<->B', to: 'other', step: 3 }
];

export interface KeyLadderRow {
	shift: number;
	key: string;
	pattern: string | null;
	desirable: boolean;
}

function _ladderPattern(landed: CamelotKey, target: CamelotKey): string | null {
	const sameMode = landed.mode === target.mode;
	const numberStep = _pitchClass(landed.number - target.number);
	const match = KEY_LADDER_PATTERNS.find(
		(p) => sameMode === (p.to === 'same') && numberStep === _pitchClass(p.to === 'other' && target.mode === 'B' ? -p.step : p.step)
	);
	return match?.label ?? null;
}

/** Rows +6 down to -6: absolute shift from the follower's original key. */
export function keyShiftLadder(followerKey: string | null, targetKey: string | null): KeyLadderRow[] {
	const [follower, target] = _parseKeySyncKeys(followerKey, targetKey);
	const rows: KeyLadderRow[] = [];
	for (let shift = 6; shift >= -6; shift -= 1) {
		const landed = shift === 0 ? follower : _shiftCamelotKey(follower, shift);
		const pattern = _ladderPattern(landed, target);
		rows.push({ shift, key: `${landed.number}${landed.mode}`, pattern, desirable: pattern !== null });
	}
	return rows;
}

//----------------------------------------------------------------- KEY SYNC status (DECKUX-34)
// `key_sync_enabled` is the operator's arm; it can outlive the master it was
// armed against (restore before any master, master handed off or reloaded,
// this deck elected master). The light reads this status instead, derived
// from published deck state every time so it cannot drift.

export type KeySyncStatus =
	| 'off'
	| 'following'
	| 'syncing'
	| 'waiting-no-master'
	| 'waiting-is-master'
	| 'waiting-no-key'
	| 'waiting-mode-mismatch';

export interface KeySyncStatusDeck {
	stable_id: string | null;
	key: string | null;
	key_guess?: string | null;
	key_sync_enabled: boolean;
	/** KEYSYNC-01: exact (default), relative or compatible. */
	key_sync_mode: KeySyncMode;
	/** KEYSYNC-01: the deck to follow; null follows the elected MASTER. */
	key_sync_target: DeckId | null;
	is_master: boolean;
}

/** The deck a KEY SYNC follower takes its key from: its target, else MASTER. */
export function keySyncLeaderDeck(
	deck: DeckId,
	decks: Readonly<Record<DeckId, KeySyncStatusDeck>>
): DeckId | null {
	const target = decks[deck].key_sync_target;
	if (target !== null) return target;
	return DECK_IDS.find((candidate) => decks[candidate].is_master) ?? null;
}

export function deriveKeySyncStatus(
	deck: DeckId,
	decks: Readonly<Record<DeckId, KeySyncStatusDeck>>
): KeySyncStatus {
	if (!DECK_IDS.includes(deck)) throw new RangeError(`KEY SYNC deck must be within 1..4, got ${deck}`);
	const source = decks[deck];
	if (!source.key_sync_enabled) return 'off';
	if (source.key_sync_target === null && source.is_master) return 'waiting-is-master';
	const leaderDeck = keySyncLeaderDeck(deck, decks);
	if (leaderDeck === null || decks[leaderDeck].stable_id === null) return 'waiting-no-master';
	const leaderKey = keySyncKey(decks[leaderDeck]);
	const sourceKey = keySyncKey(source);
	if (source.stable_id === null || parseCamelotKey(sourceKey) === null || parseCamelotKey(leaderKey) === null) {
		return 'waiting-no-key';
	}
	if (keySyncModeMismatch(sourceKey, leaderKey, source.key_sync_mode)) return 'waiting-mode-mismatch';
	return 'following';
}

/** Narrow 'following' to what the deck actually PLAYS: it holds only when a
 * plan is derivable and the presented shift already sits on its target
 * (delta 0). Otherwise the follow is still being applied ('syncing') and the
 * light must stay off. The engine passes `keySyncPreview(deck).deltaSemitones`,
 * or null when no plan is derivable yet (live deck before presentation). */
export function refineKeySyncStatus(
	status: KeySyncStatus,
	presentedDeltaSemitones: number | null
): KeySyncStatus {
	if (status !== 'following') return status;
	return presentedDeltaSemitones === 0 ? 'following' : 'syncing';
}

/** Hover copy for each status, shared by the deck header and agents. */
export function keySyncStatusTitle(status: Exclude<KeySyncStatus, 'off' | 'following'>): string {
	switch (status) {
		case 'syncing':
			return "KEY SYNC ARMED - not following yet: the master's key is still being applied to the presented audio.";
		case 'waiting-no-master':
			return 'KEY SYNC ARMED - not following: no loaded master deck yet. Applies when a master is selected.';
		case 'waiting-is-master':
			return 'KEY SYNC ARMED - not following: this deck is the master. Applies again when another deck is master.';
		case 'waiting-no-key':
			return 'KEY SYNC ARMED - not following: this deck or the master has no Camelot key.';
		case 'waiting-mode-mismatch':
			return 'KEY SYNC ARMED - not following: exact needs both keys in the same mode (A or B). Right-click KEY SYNC and pick relative to land on the relative major/minor.';
		default: {
			const _exhaustive: never = status;
			throw new Error(`Unhandled KEY SYNC status: ${String(_exhaustive)}`);
		}
	}
}

export type { CamelotRuleId } from './camelot-rules.generated';

/** True when `candidate` is one of the enabled rules' moves away from
 * `reference` (compatible filter checkboxes). The rule table is generated
 * from apps/shared/harmonic.py CAMELOT_RULES, so the browser and the server
 * agree on what every rule means. Every rule respects the A/B letter and
 * wraps 12 <-> 1. A key that does not parse matches nothing. Every rule id is
 * resolved before any is tried, so an unknown one always throws. */
export function camelotRulesAllow(
	reference: string | null,
	candidate: string | null,
	ruleIds: readonly CamelotRuleId[]
): boolean {
	const rules = ruleIds.map((id) => {
		const rule = CAMELOT_RULES_BY_ID.get(id);
		if (rule === undefined) throw new Error(`Unknown Camelot rule: ${id}`);
		return rule;
	});
	const ref = parseCamelotKey(reference);
	const cand = parseCamelotKey(candidate);
	if (ref === null || cand === null) return false;
	return rules.some((rule) =>
		rule.moves.some(
			(move) =>
				(move.from === '*' || move.from === ref.mode) &&
				(cand.mode === ref.mode) === (move.to === 'same') &&
				(((ref.number - 1 + move.step) % 12) + 12) % 12 === cand.number - 1
		)
	);
}

const CAMELOT_RULES_BY_ID: ReadonlyMap<CamelotRuleId, CamelotRule> = new Map(
	CAMELOT_RULES.map((rule) => [rule.id, rule])
);

export const CAMELOT_RULE_IDS: readonly CamelotRuleId[] = CAMELOT_RULES.map((rule) => rule.id);
export const DEFAULT_CAMELOT_RULE_IDS: readonly CamelotRuleId[] = CAMELOT_RULES.filter((r) => r.default_on).map(
	(r) => r.id
);

export function isCamelotRuleId(value: unknown): value is CamelotRuleId {
	return typeof value === 'string' && (CAMELOT_RULE_IDS as readonly string[]).includes(value);
}
