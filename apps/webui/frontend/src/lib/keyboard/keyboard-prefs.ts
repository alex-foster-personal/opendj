/**
 * KEYB-11488: keyboard layout preset and per-command override prefs.
 */
import type { KeymapOverrides } from './keymap';
import { isKeymapPresetId, type KeymapPresetId } from './presets/index';

export interface KeyboardPrefsValue {
	preset: KeymapPresetId;
	overrides: Record<string, string[]>;
}

export interface KeyboardPrefsState {
	keyboard: KeyboardPrefsValue;
}

export const KEYBOARD_PREF_DEFAULTS: KeyboardPrefsValue = {
	preset: 'opendj',
	overrides: {}
};

function parseOverrides(raw: unknown): Record<string, string[]> {
	if (raw === undefined) return {};
	if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
		throw new Error('keyboard.overrides must be an object');
	}
	const out: Record<string, string[]> = {};
	for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
		if (!Array.isArray(value) || !value.every((v) => typeof v === 'string')) {
			throw new Error(`keyboard.overrides.${key} must be a list of strings`);
		}
		out[key] = value;
	}
	return out;
}

export function parseKeyboardPrefs(value: unknown): KeyboardPrefsValue | undefined {
	if (value === undefined) return undefined;
	if (typeof value !== 'object' || value === null || Array.isArray(value)) {
		throw new Error('keyboard must be an object');
	}
	const raw = value as Record<string, unknown>;
	for (const key of Object.keys(raw)) {
		if (key !== 'preset' && key !== 'overrides') {
			throw new Error(`keyboard has unknown key: ${key}`);
		}
	}
	const presetRaw = raw.preset ?? 'opendj';
	if (!isKeymapPresetId(presetRaw)) {
		throw new Error(`keyboard.preset must be opendj|rekordbox|serato, got ${String(presetRaw)}`);
	}
	return { preset: presetRaw, overrides: parseOverrides(raw.overrides) };
}

export function validateKeyboardPrefFields(
	parsed: { keyboard?: unknown },
	storageKey: string
): Partial<KeyboardPrefsState> {
	if (parsed.keyboard === undefined) return {};
	try {
		const keyboard = parseKeyboardPrefs(parsed.keyboard);
		if (keyboard === undefined) return {};
		return { keyboard };
	} catch (err) {
		const msg = err instanceof Error ? err.message : String(err);
		throw new Error(
			`${storageKey}: malformed prefs blob (${msg}) - clear the localStorage key to recover`
		);
	}
}

export interface KeyboardSetters {
	setKeyboardPreset(next: KeymapPresetId): void;
	setKeyboardOverrides(next: KeymapOverrides): void;
}

export function makeKeyboardSetters(
	state: KeyboardPrefsState,
	persist: () => void,
	syncDiskPrefs: (patch: { keyboard?: KeyboardPrefsValue }) => void
): KeyboardSetters {
	return {
		setKeyboardPreset(next: KeymapPresetId): void {
			if (!isKeymapPresetId(next)) {
				throw new Error(`keyboard.preset must be opendj|rekordbox|serato, got ${String(next)}`);
			}
			state.keyboard = { preset: next, overrides: { ...state.keyboard.overrides } };
			persist();
			syncDiskPrefs({ keyboard: state.keyboard });
		},
		setKeyboardOverrides(next: KeymapOverrides): void {
			const overrides: Record<string, string[]> = {};
			for (const [id, chords] of Object.entries(next)) {
				overrides[id] = [...chords];
			}
			state.keyboard = { preset: state.keyboard.preset, overrides };
			persist();
			syncDiskPrefs({ keyboard: state.keyboard });
		}
	};
}
