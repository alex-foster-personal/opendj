/**
 * Camelot key algebra and KEY SYNC derivation. Pure: no runtime, no stores, no
 * audio nodes. Extracted verbatim from audio-engine.svelte.ts (T4 S1).
 *
 * DESIGN DRIFT: `masterTempoSemitones` lands here, not in key/key-shift.ts as
 * the T4 module table assigns it. `composeStretchSemitones` calls it, and the
 * design's own diagram points key-shift -> camelot, so the assigned split is a
 * cycle. Arithmetic unchanged.
 */

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
 * Accepts numbered Camelot ("8A", "12b") and standard musical notation
 * ("Gm" -> 6A, "F#" -> 2B, "Ab major" -> 4B, "Bbm" -> 3A). Returns null for
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

/** Convert a listener-facing KEY SYNC nudge to the absolute manual schedule value. */
export function deriveKeySyncTargetManualShift(
	deckKey: string | null,
	masterKey: string | null,
	deckEffectiveAudibleSemitones: number,
	masterEffectiveAudibleSemitones: number,
	presentedManualShiftSemitones: number
): number {
	_assertKeyShift(presentedManualShiftSemitones);
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
	_assertKeyShift(keyShiftSemitones);
	return masterTempoSemitones(tempoRatio, masterTempoEnabled) + keyShiftSemitones;
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
	| 'waiting-no-key';

export interface KeySyncStatusDeck {
	stable_id: string | null;
	key: string | null;
	key_sync_enabled: boolean;
	is_master: boolean;
}

export function deriveKeySyncStatus(
	deck: DeckId,
	decks: Readonly<Record<DeckId, KeySyncStatusDeck>>
): KeySyncStatus {
	if (!DECK_IDS.includes(deck)) throw new RangeError(`KEY SYNC deck must be within 1..4, got ${deck}`);
	const source = decks[deck];
	if (!source.key_sync_enabled) return 'off';
	if (source.is_master) return 'waiting-is-master';
	const masterDeck = DECK_IDS.find((candidate) => decks[candidate].is_master);
	if (masterDeck === undefined || decks[masterDeck].stable_id === null) return 'waiting-no-master';
	const master = decks[masterDeck];
	if (source.stable_id === null || parseCamelotKey(source.key) === null || parseCamelotKey(master.key) === null) {
		return 'waiting-no-key';
	}
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
		default: {
			const _exhaustive: never = status;
			throw new Error(`Unhandled KEY SYNC status: ${String(_exhaustive)}`);
		}
	}
}
