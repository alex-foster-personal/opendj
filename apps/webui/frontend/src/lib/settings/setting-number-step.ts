/**
 * CUSTSCR-10464 (issue #10202): pure step/clamp/parse rules for number setting steppers.
 *
 * Requirements (mini-PRD):
 *   ✔︎ nudging at min or max returns null so the host never calls applySettingChange.
 *     [if] stepSettingNumber is called at max with a positive step [then ⛔️] it returns null
 *   ✔︎ ArrowUp/ArrowDown map to ±1 step, ×10 with Shift; other keys map to null.
 *     [if] stepperKeySteps sees Enter [then ⛔️] it returns null
 *   ✔︎ typed values outside [min, max] are rejected, never clamped.
 *     [if] parseStepperText gets 101 on a 0..100 control [then ⛔️] it returns null
 */
import type { SettingControl } from './catalog-types';

export type NumberControl = Extract<SettingControl, { kind: 'number' }>;

export function formatSettingNumber(value: number, step: number): string {
	return value.toFixed(Number.isInteger(step) ? 0 : 2);
}

function _decimals(step: number): number {
	const parts = String(step).split('.');
	return parts.length === 2 ? parts[1].length : 0;
}

export function stepperKeySteps(key: string, shift: boolean): number | null {
	let steps: number | null;
	if (key === 'ArrowUp') steps = 1;
	else if (key === 'ArrowDown') steps = -1;
	else return null;
	return shift ? steps * 10 : steps;
}

export function stepSettingNumber(
	control: NumberControl,
	value: number,
	steps: number
): number | null {
	if (!Number.isFinite(value))
		throw new Error(`stepSettingNumber: value must be finite, got ${String(value)}`);
	const raw = value + steps * control.step;
	let clamped = Math.min(control.max, Math.max(control.min, raw));
	const rounded = Number(clamped.toFixed(_decimals(control.step)));
	clamped = Math.min(control.max, Math.max(control.min, rounded));
	if (clamped === value) return null;
	return clamped;
}

export function parseStepperText(control: NumberControl, text: string): number | null {
	let trimmed = text.trim();
	const unit = control.unit.trim();
	if (unit.length > 0 && trimmed.endsWith(unit)) {
		trimmed = trimmed.slice(0, -unit.length).trim();
	}
	if (trimmed.length === 0) return null;
	const parsed = Number(trimmed);
	if (!Number.isFinite(parsed)) return null;
	if (parsed < control.min || parsed > control.max) return null;
	return parsed;
}
