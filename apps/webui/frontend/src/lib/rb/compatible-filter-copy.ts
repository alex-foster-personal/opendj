/**
 * User-facing copy for the compatible filter's modes and key shifts
 * (KEYSHIFT-01..04). Pure formatting; the matching lives in
 * player/key/camelot.ts (compatibleKeyShift).
 */
import { parseCamelotKey, type CamelotRuleId } from '$lib/player/key/camelot';
import { CAMELOT_RULES } from '$lib/player/key/camelot-rules.generated';
import { displayKey, type KeyNotation } from '$lib/rb/key-notation';
import type { CompatibleEnergyMode, CompatibleFilterPrefs } from '$lib/rb/compatible-filter-prefs';

/** Pitch class -> lettered key (C = 0), the spelling the key hover uses. */
const NOTE_LETTERS = ['C', 'C#', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B'] as const;

function _semitones(n: number): string {
	return `${n} semitone${n === 1 ? '' : 's'}`;
}

/** One line saying what the active mode matches. */
export function compatibleModeDefinition(prefs: CompatibleFilterPrefs): string {
	const shift = prefs.key_shift_max === 0 ? 'with no key shift' : `after a shift of up to ${_semitones(prefs.key_shift_max)}`;
	if (prefs.mode === 'mashup') {
		return `The exact same key${prefs.mashup_relative ? ' or its relative major/minor' : ''}, ${shift}.`;
	}
	if (prefs.rules.length === 0) return 'Key not checked (no Camelot rule ticked): BPM alone decides.';
	return `${compatibleRuleLabels(prefs.rules)}, ${shift}.`;
}

/** The ticked Camelot rules' labels in table order, or "key not checked". */
export function compatibleRuleLabels(rules: readonly CamelotRuleId[]): string {
	const on = CAMELOT_RULES.filter((rule) => rules.includes(rule.id));
	return on.length === 0 ? 'key not checked' : on.map((rule) => rule.label).join(', ');
}

/** The dropdown's one-line summary of the current settings. */
export function compatibleFilterSummary(prefs: CompatibleFilterPrefs): string {
	const mode = prefs.mode === 'mashup' ? 'Mashup' : `Transition (${compatibleRuleLabels(prefs.rules)})`;
	const shift = prefs.key_shift_max === 0 ? 'no shift' : `shift ±${prefs.key_shift_max}`;
	const bpm = prefs.bpm_enabled ? `BPM ±${prefs.bpm_window_bpm} (${prefs.bpm_direction})` : 'BPM off';
	const energy =
		prefs.energy_mode !== undefined && prefs.energy_mode !== 'off'
			? `, ${compatibleEnergyLabel(prefs.energy_mode)}`
			: '';
	return `${mode}, ${shift}, ${bpm}${energy}`;
}

/** SETPREP-11947: the energy rule's short label. */
export function compatibleEnergyLabel(mode: CompatibleEnergyMode): string {
	switch (mode) {
		case 'within_1':
			return 'energy within 1';
		case 'boost':
			return 'energy boost +1 to +2';
		case 'drop':
			return 'energy drop -1 to -2';
		case 'off':
			return 'energy off';
	}
}

/** SETPREP-11947: one sentence per energy mode (the panel button titles). */
export function compatibleEnergyDefinition(mode: CompatibleEnergyMode): string {
	switch (mode) {
		case 'within_1':
			return 'Energy one level either side of the reference track.';
		case 'boost':
			return "Energy one or two levels above the reference track (Mixed In Key's Energy Boost).";
		case 'drop':
			return 'Energy one or two levels below the reference track.';
		case 'off':
			return 'Energy is not checked.';
	}
}

/** The shifted key a row will play in, in the user's key notation (KEYSHIFT-03).
 * `camelotKey` comes from compatibleKeyShift, so it always parses; a key that
 * does not is a contract failure and throws, never shown as Camelot instead. */
export function shiftedKeyText(camelotKey: string, notation: KeyNotation): string {
	const shown = displayKey(camelotKey, notation);
	if (shown === null || !shown.parsed) {
		throw new Error(`shiftedKeyText: shifted key ${JSON.stringify(camelotKey)} does not render in ${notation}`);
	}
	return shown.text;
}

/** Superscript text for a load shift: "+1", "-1" (with U+2212 minus), or "". */
export function keyShiftSuperscript(shift: number): string {
	if (shift === 0) return '';
	return `${shift > 0 ? '+' : '−'}${Math.abs(shift)}`;
}

/** Lettered key ("Eb", "Am") and Camelot key ("2A") for the key hover card,
 * or null when the key does not parse. Minor keys carry a lowercase m. */
export function letteredAndCamelot(key: string | null): { lettered: string; quality: string; camelot: string } | null {
	const parsed = parseCamelotKey(key);
	if (parsed === null) return null;
	const minor = parsed.mode === 'A';
	const note = NOTE_LETTERS[parsed.root];
	return {
		lettered: `${note}${minor ? 'm' : ''}`,
		quality: `${note} ${minor ? 'minor' : 'major'}`,
		camelot: `${parsed.number}${parsed.mode}`
	};
}

/** The shift line under a shifted row's key hover. */
export function keyShiftHoverLine(ownKey: string | null, shift: number): string | null {
	if (shift === 0) return null;
	const own = letteredAndCamelot(ownKey);
	if (own === null) return null;
	return `Its own key: ${own.lettered} · ${own.camelot}. Loads ${shift > 0 ? 'up' : 'down'} ${_semitones(Math.abs(shift))}.`;
}
