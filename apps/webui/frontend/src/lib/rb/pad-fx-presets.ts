/**
 * Pad FX preset banks (PADFX-11718): the schema check and bank lookup for
 * `pad-fx-presets.json`, the data file a taste pass edits to change what the
 * eight pads of Pad FX 1 and Pad FX 2 do. No code change is needed for that.
 *
 * Mimicked: rekordbox 7.2.8 Instruction Manual p. 128 and pp. 182-183 (two
 * banks, hold applies the effect, Release FX pads). Open DJ's bank has 8 pads,
 * so each bank holds 8 presets.
 *
 * A preset naming an effect that is not in BEAT_FX_EFFECTS yet (the registry)
 * is inert with a stated reason; it comes live when a kernel PR registers it.
 * The file is parsed at module load and a malformed entry throws, naming its path.
 */
import rawPresetFile from './pad-fx-presets.json';
import {
	BEAT_FX_BEATS,
	BEAT_FX_EFFECTS,
	BEAT_FX_RELEASE_KINDS,
	type BeatFxBeats,
	type BeatFxEffect
} from './beat-fx';

export const PAD_FX_MODE_IDS = ['pad-fx-1', 'pad-fx-2'] as const;
export type PadFxModeId = (typeof PAD_FX_MODE_IDS)[number];
export const PAD_FX_BANK_SIZE = 8;
export const PAD_FX_LABEL_MAX = 12;
/** Effect names a preset may name: the beat FX the fan-out plans (fx-fanout-20261108).
 *  Closed so a typo fails the load instead of becoming a silent inert pad. */
export const PAD_FX_PRESET_EFFECTS = [
	'echo',
	'roll',
	'trans',
	'filter',
	'reverb',
	'flanger',
	'phaser',
	'delay'
] as const;
export type PadFxPresetEffect = (typeof PAD_FX_PRESET_EFFECTS)[number];

export type PadFxBeatPreset =
	| { kind: 'beat'; label: string; effect: PadFxPresetEffect; beats: BeatFxBeats }
	| { kind: 'beat'; label: string; effect: PadFxPresetEffect; amount: number };
export interface PadFxReleasePreset {
	kind: 'release';
	label: string;
	release: string;
	beats: BeatFxBeats;
	hold: boolean;
}
export type PadFxPreset = PadFxBeatPreset | PadFxReleasePreset;
export interface PadFxPresetFile {
	version: 1;
	note: string;
	banks: Record<PadFxModeId, readonly PadFxPreset[]>;
}
export type PadFxPresetReadiness = { live: true } | { live: false; reason: string };

//-----------------------------------------------------------------------------
// parse helpers

function _record(path: string, value: unknown): Record<string, unknown> {
	if (typeof value !== 'object' || value === null || Array.isArray(value))
		throw new TypeError(`${path} must be an object; got ${JSON.stringify(value)}`);
	return value as Record<string, unknown>;
}

function _exactKeys(path: string, record: Record<string, unknown>, keys: readonly string[]): void {
	for (const key of Object.keys(record))
		if (!keys.includes(key)) throw new TypeError(`${path}.${key} is an unknown field`);
	for (const key of keys) if (!(key in record)) throw new TypeError(`${path}.${key} is missing`);
}

function _oneOf<T>(path: string, value: unknown, allowed: readonly T[]): T {
	if (!(allowed as readonly unknown[]).includes(value))
		throw new RangeError(
			`${path} must be one of ${allowed.join('|')}; got ${JSON.stringify(value)}`
		);
	return value as T;
}

function _text(path: string, value: unknown, max: number): string {
	if (typeof value !== 'string' || value.trim() === '' || value.length > max)
		throw new TypeError(
			`${path} must be a non-empty string of at most ${max} characters; got ${JSON.stringify(value)}`
		);
	return value;
}

function _parsePreset(path: string, value: unknown): PadFxPreset {
	const entry = _record(path, value);
	const kind = _oneOf(`${path}.kind`, entry.kind, ['beat', 'release'] as const);
	const label = _text(`${path}.label`, entry.label, PAD_FX_LABEL_MAX);
	if (kind === 'release') {
		_exactKeys(path, entry, ['label', 'kind', 'release', 'beats', 'hold']);
		if (typeof entry.hold !== 'boolean')
			throw new TypeError(`${path}.hold must be boolean; got ${JSON.stringify(entry.hold)}`);
		return {
			kind,
			label,
			release: _text(`${path}.release`, entry.release, 32),
			beats: _oneOf(`${path}.beats`, entry.beats, BEAT_FX_BEATS),
			hold: entry.hold
		};
	} else if (kind === 'beat') {
		const effect = _oneOf(`${path}.effect`, entry.effect, PAD_FX_PRESET_EFFECTS);
		if ('beats' in entry && 'amount' in entry)
			throw new TypeError(`${path} must have exactly one of beats or amount; got both`);
		if ('amount' in entry) {
			_exactKeys(path, entry, ['label', 'kind', 'effect', 'amount']);
			const amount = entry.amount;
			if (typeof amount !== 'number' || !Number.isInteger(amount) || amount < 1 || amount > 100)
				throw new RangeError(
					`${path}.amount must be an integer in 1..100; got ${JSON.stringify(amount)}`
				);
			return { kind, label, effect, amount };
		}
		_exactKeys(path, entry, ['label', 'kind', 'effect', 'beats']);
		return { kind, label, effect, beats: _oneOf(`${path}.beats`, entry.beats, BEAT_FX_BEATS) };
	}
	const _exhaustive: never = kind;
	throw new Error(`Unhandled preset kind: ${String(_exhaustive)}`);
}

//-----------------------------------------------------------------------------
// public

/** Validate a whole preset file. Throws naming the entry path, field and bad value. */
export function parsePadFxPresetFile(raw: unknown): PadFxPresetFile {
	const file = _record('pad-fx-presets', raw);
	_exactKeys('pad-fx-presets', file, ['version', 'note', 'banks']);
	if (file.version !== 1)
		throw new RangeError(`pad-fx-presets.version must be 1; got ${JSON.stringify(file.version)}`);
	const note = _text('pad-fx-presets.note', file.note, 1000);
	const rawBanks = _record('banks', file.banks);
	_exactKeys('banks', rawBanks, PAD_FX_MODE_IDS);
	const banks = {} as Record<PadFxModeId, readonly PadFxPreset[]>;
	for (const mode of PAD_FX_MODE_IDS) {
		const list = rawBanks[mode];
		if (!Array.isArray(list) || list.length !== PAD_FX_BANK_SIZE)
			throw new RangeError(
				`banks.${mode} must be an array of exactly ${PAD_FX_BANK_SIZE} presets; got ${Array.isArray(list) ? list.length : JSON.stringify(list)}`
			);
		banks[mode] = list.map((entry, i) => _parsePreset(`banks.${mode}[${i}]`, entry));
	}
	return { version: 1, note, banks };
}

const DEFAULT_FILE = parsePadFxPresetFile(rawPresetFile as unknown);

export function isPadFxModeId(id: string): id is PadFxModeId {
	return (PAD_FX_MODE_IDS as readonly string[]).includes(id);
}

export function padFxBank(mode: PadFxModeId): readonly PadFxPreset[] {
	return DEFAULT_FILE.banks[mode];
}

/** Live = a `beats` preset on a registered effect, or a registered release kind. */
export function padFxPresetReadiness(preset: PadFxPreset): PadFxPresetReadiness {
	if (preset.kind === 'release') {
		if (!(BEAT_FX_RELEASE_KINDS as readonly string[]).includes(preset.release))
			return {
				live: false,
				reason: `${preset.label}: release ${preset.release} is not a beat FX release kind yet`
			};
		return { live: true };
	} else if (preset.kind === 'beat') {
		if (!(BEAT_FX_EFFECTS as readonly string[]).includes(preset.effect))
			return {
				live: false,
				reason: `${preset.label}: effect ${preset.effect} is not in the beat FX registry yet`
			};
		if ('amount' in preset)
			return { live: false, reason: `${preset.label}: beat FX has no amount parameter yet` };
		return { live: true };
	}
	const _exhaustive: never = preset;
	throw new Error(`Unhandled preset: ${JSON.stringify(_exhaustive)}`);
}

/** The division list the unit steps through for a registered effect. */
export function beatFxDivisionsFor(effect: BeatFxEffect): readonly BeatFxBeats[] {
	_oneOf('beat fx effect', effect, BEAT_FX_EFFECTS);
	return BEAT_FX_BEATS;
}
