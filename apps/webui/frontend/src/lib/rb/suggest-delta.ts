/**
 * Display helpers for Suggest Next: signed BPM / Camelot deltas as (+-X)
 * instead of tags like bpm_match / camelot_step_0.
 */

import { parseCamelotKey } from '$lib/rb/audio-engine.svelte';

export function formatSigned(n: number, digits = 0): string {
	const abs = Math.abs(n);
	const body = digits > 0 ? abs.toFixed(digits) : String(Math.round(abs));
	if (n > 0) return `(+${body})`;
	if (n < 0) return `(-${body})`;
	return `(±${body})`;
}

/** Signed BPM delta of candidate vs reference (absolute BPM, 1 decimal). */
export function bpmDeltaLabel(
	candidateBpm: number | null,
	referenceBpm: number | null
): string | null {
	if (candidateBpm === null || referenceBpm === null) return null;
	if (!Number.isFinite(candidateBpm) || !Number.isFinite(referenceBpm)) return null;
	return formatSigned(candidateBpm - referenceBpm, 1);
}

/**
 * Signed Camelot number step (shortest wheel distance). Mode change alone
 * is still (±0) on the number wheel - matches camelot_step_0 semantics.
 */
export function camelotStepLabel(
	candidateKey: string | null,
	referenceKey: string | null
): string | null {
	const cand = parseCamelotKey(candidateKey);
	const ref = parseCamelotKey(referenceKey);
	if (cand === null || ref === null) return null;
	let d = cand.number - ref.number;
	if (d > 6) d -= 12;
	if (d < -6) d += 12;
	return formatSigned(d, 0);
}

/** Tags that are superseded by the (±X) BPM/key readout. */
const REDUNDANT_TAGS = new Set([
	'bpm_match',
	'bpm_close',
	'camelot_step_0',
	'camelot_step_1_2'
]);

export function filterSuggestTags(tags: string[]): string[] {
	return tags.filter((t) => !REDUNDANT_TAGS.has(t));
}
