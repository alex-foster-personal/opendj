/**
 * Beat FX unit, pure core (BEATFX-10): the AudioContext-free half of the one
 * channel-select beat FX unit. The `beat_fx` and `beat_fx_release` wire shapes
 * and their validation, the beat division list, the tempo source rule and the
 * quantize boundary math. The Web Audio graph and the panel build on this.
 *
 * Spec: `(private path)` sections 4.1, 4.3, 4.4.
 * Adapted from our own master-only rack on `af--b2b-link` (`beat-fx.ts`).
 * Mimicked: DJM CH SELECT targets 1 to 4 or master, division range 1/16 to 16
 * beats (DJM-A9 manual p. 52); 3/4 kept from main's color echo.
 */
import { COLOR_FX_MAX_BPM, COLOR_FX_MIN_BPM } from './color-fx';

/** Channel select: one of decks 1 to 4, or the master bus. */
export const BEAT_FX_TARGETS = ['1', '2', '3', '4', 'master'] as const;
export type BeatFxTarget = (typeof BEAT_FX_TARGETS)[number];
export type BeatFxDeckTarget = Exclude<BeatFxTarget, 'master'>;

/** Beat divisions the unit's beat buttons and X-PAD step through, in order. */
export const BEAT_FX_BEATS = [1 / 16, 1 / 8, 1 / 4, 1 / 2, 3 / 4, 1, 2, 4, 8, 16] as const;
export type BeatFxBeats = (typeof BEAT_FX_BEATS)[number];

/** Kernels this round ships; later kernel PRs add their names here. */
export const BEAT_FX_EFFECTS = ['echo'] as const;
export type BeatFxEffect = (typeof BEAT_FX_EFFECTS)[number];

/** Release FX kinds this round ships. */
export const BEAT_FX_RELEASE_KINDS = ['echo'] as const;
export type BeatFxReleaseKind = (typeof BEAT_FX_RELEASE_KINDS)[number];

/** Tempo range the unit accepts; the same range as color ECHO. */
export const BEAT_FX_MIN_BPM = COLOR_FX_MIN_BPM;
export const BEAT_FX_MAX_BPM = COLOR_FX_MAX_BPM;

/** The `beat_fx` command: every field optional, at least one present.
 * `xpad` is a touch at 0..1 across the pad, or null for the release. */
export interface BeatFxCommand {
	type: 'beat_fx';
	target?: BeatFxTarget;
	effect?: BeatFxEffect;
	beats?: BeatFxBeats;
	level?: number;
	enabled?: boolean;
	quantize?: boolean;
	xpad?: number | null;
}

/** The `beat_fx_release` command: hold or let go of Release FX. */
export interface BeatFxReleaseCommand {
	type: 'beat_fx_release';
	held: boolean;
	beats?: BeatFxBeats;
	kind?: BeatFxReleaseKind;
}

/** One deck as the tempo source rule sees it. */
export interface BeatFxDeckTempo {
	deck: BeatFxDeckTarget;
	/** `deckEffectiveBpm` for this deck, or null with no tempo. */
	effectiveBpm: number | null;
	playing: boolean;
	isMaster: boolean;
}

const BEAT_FX_FIELDS = new Set([
	'type',
	'target',
	'effect',
	'beats',
	'level',
	'enabled',
	'quantize',
	'xpad'
]);
const BEAT_FX_RELEASE_FIELDS = new Set(['type', 'held', 'beats', 'kind']);

//-----------------------------------------------------------------------------
// validation helpers

function _assertKnownFields(
	command: string,
	record: Record<string, unknown>,
	allowed: Set<string>
): void {
	for (const key of Object.keys(record)) {
		if (!allowed.has(key)) throw new TypeError(`${command} has unknown field ${key}`);
	}
}

function _oneOf<T>(command: string, field: string, value: unknown, allowed: readonly T[]): T {
	if (!(allowed as readonly unknown[]).includes(value)) {
		throw new TypeError(
			`${command} ${field} must be one of ${allowed.join('|')}; got ${String(value)}`
		);
	}
	return value as T;
}

function _boolean(command: string, field: string, value: unknown): boolean {
	if (typeof value !== 'boolean')
		throw new TypeError(`${command} ${field} must be boolean; got ${String(value)}`);
	return value;
}

function _unit(command: string, field: string, value: unknown): number {
	if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 1) {
		throw new RangeError(`${command} ${field} must be a number in 0..1; got ${String(value)}`);
	}
	return value;
}

function _assertFiniteNonNegative(field: string, value: number): void {
	if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
		throw new RangeError(`beat fx ${field} must be a finite number >= 0; got ${String(value)}`);
	}
}

function _inTempoRange(bpm: number | null): number | null {
	return typeof bpm === 'number' && bpm >= BEAT_FX_MIN_BPM && bpm <= BEAT_FX_MAX_BPM ? bpm : null;
}

//-----------------------------------------------------------------------------
// commands

/** Validate a `beat_fx` command off the wire. Throws with the reason, never guesses. */
export function parseBeatFxCommand(record: Record<string, unknown>): BeatFxCommand {
	const name = 'beat_fx';
	_assertKnownFields(name, record, BEAT_FX_FIELDS);
	if (record.type !== name)
		throw new TypeError(`beat_fx type must be beat_fx; got ${String(record.type)}`);
	const out: BeatFxCommand = { type: name };
	if (record.target !== undefined)
		out.target = _oneOf(name, 'target', record.target, BEAT_FX_TARGETS);
	if (record.effect !== undefined)
		out.effect = _oneOf(name, 'effect', record.effect, BEAT_FX_EFFECTS);
	if (record.beats !== undefined) out.beats = _oneOf(name, 'beats', record.beats, BEAT_FX_BEATS);
	if (record.level !== undefined) out.level = _unit(name, 'level', record.level);
	if (record.enabled !== undefined) out.enabled = _boolean(name, 'enabled', record.enabled);
	if (record.quantize !== undefined) out.quantize = _boolean(name, 'quantize', record.quantize);
	if (record.xpad !== undefined)
		out.xpad = record.xpad === null ? null : _unit(name, 'xpad', record.xpad);
	if (Object.keys(out).length === 1) {
		throw new TypeError(
			'beat_fx needs at least one of target, effect, beats, level, enabled, quantize, xpad'
		);
	}
	return out;
}

/** Validate a `beat_fx_release` command off the wire. Throws with the reason. */
export function parseBeatFxReleaseCommand(record: Record<string, unknown>): BeatFxReleaseCommand {
	const name = 'beat_fx_release';
	_assertKnownFields(name, record, BEAT_FX_RELEASE_FIELDS);
	if (record.type !== name)
		throw new TypeError(`beat_fx_release type must be beat_fx_release; got ${String(record.type)}`);
	const out: BeatFxReleaseCommand = { type: name, held: _boolean(name, 'held', record.held) };
	if (record.beats !== undefined) out.beats = _oneOf(name, 'beats', record.beats, BEAT_FX_BEATS);
	if (record.kind !== undefined)
		out.kind = _oneOf(name, 'kind', record.kind, BEAT_FX_RELEASE_KINDS);
	return out;
}

//-----------------------------------------------------------------------------
// tempo, quantize, X-PAD

/**
 * The tempo the unit times its kernel by. A deck target uses that deck's
 * effective BPM; master uses the master deck's, else the first playing deck's
 * (deck order). Null when there is none or it is outside 40..300 BPM: a
 * tempo-timed kernel is then refused by the caller, never guessed.
 */
export function beatFxTempoSource(
	target: BeatFxTarget,
	decks: readonly BeatFxDeckTempo[]
): number | null {
	_oneOf('beat fx', 'target', target, BEAT_FX_TARGETS);
	if (target !== 'master')
		return _inTempoRange(decks.find((d) => d.deck === target)?.effectiveBpm ?? null);
	const ordered = [...decks].sort((a, b) => Number(a.deck) - Number(b.deck));
	const source = ordered.find((d) => d.isMaster) ?? ordered.find((d) => d.playing);
	return _inTempoRange(source?.effectiveBpm ?? null);
}

/**
 * The next quantize boundary at or after `nowSec`: a boundary of
 * min(`beats`, 1 beat) of the grid, so the wait is never more than one beat.
 * `gridBeatsSec` are the target's beat times in seconds, ascending. Null with
 * no grid, or when `nowSec` is off the grid (before the first beat or at or
 * after the last): the unit then engages at once in the `no_grid` state.
 */
export function nextQuantizeBoundarySec(
	gridBeatsSec: readonly number[],
	nowSec: number,
	beats: number
): number | null {
	_assertFiniteNonNegative('nowSec', nowSec);
	const division = _oneOf('beat fx', 'beats', beats, BEAT_FX_BEATS);
	if (
		gridBeatsSec.length < 2 ||
		nowSec < gridBeatsSec[0] ||
		nowSec >= gridBeatsSec[gridBeatsSec.length - 1]
	)
		return null;
	let lo = 0;
	let hi = gridBeatsSec.length - 1;
	while (hi - lo > 1) {
		const mid = (lo + hi) >> 1;
		if (gridBeatsSec[mid] <= nowSec) lo = mid;
		else hi = mid;
	}
	const beatStart = gridBeatsSec[lo];
	const span = gridBeatsSec[lo + 1] - beatStart;
	if (!(span > 0)) throw new RangeError(`beat fx grid is not ascending at beat ${lo}`);
	const step = Math.min(division, 1);
	const k = Math.ceil((nowSec - beatStart) / (span * step) - 1e-9);
	return Math.min(beatStart + k * step * span, gridBeatsSec[lo + 1]);
}

/** X-PAD position 0..1 to a beat division, across `BEAT_FX_BEATS` in order. */
export function xpadToBeats(x: number): BeatFxBeats {
	_unit('beat fx', 'xpad', x);
	return BEAT_FX_BEATS[Math.min(Math.floor(x * BEAT_FX_BEATS.length), BEAT_FX_BEATS.length - 1)];
}
