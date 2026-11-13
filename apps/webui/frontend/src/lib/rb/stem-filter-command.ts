/**
 * Pure parser for the `stem_filter {deck, stem, value}` command payload (#10326).
 * Wiring into the performance IPC union, the dispatcher and the CLI lives elsewhere.
 */
import { STEM_CONTROL_IDS, type StemControl } from './stem-types';

export interface StemFilterCommand {
	type: 'stem_filter';
	deck: number;
	stem: StemControl;
	value: number;
}

export type StemFilterParse =
	{ ok: true; command: StemFilterCommand } | { ok: false; reason: string };

/** Validate a raw stem_filter payload into a typed command, or say why it is rejected. */
export function parseStemFilterCommand(raw: unknown): StemFilterParse {
	if (raw === null || typeof raw !== 'object')
		return { ok: false, reason: 'payload must be an object' };
	const r = raw as Record<string, unknown>;
	if (typeof r.deck !== 'number' || !Number.isInteger(r.deck) || r.deck < 1) {
		return { ok: false, reason: 'deck must be an integer >= 1' };
	}
	if (typeof r.stem !== 'string' || !STEM_CONTROL_IDS.includes(r.stem as StemControl)) {
		return { ok: false, reason: `unknown stem: ${String(r.stem)}` };
	}
	if (typeof r.value !== 'number' || !Number.isFinite(r.value) || r.value < 0 || r.value > 1) {
		return { ok: false, reason: 'value must be a number in 0..1' };
	}
	return {
		ok: true,
		command: { type: 'stem_filter', deck: r.deck, stem: r.stem as StemControl, value: r.value }
	};
}
