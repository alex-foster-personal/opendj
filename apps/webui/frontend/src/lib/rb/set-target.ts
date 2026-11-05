/**
 * Set target length, skip-in-set and key clashes: the pure rules (LIBUX-8679).
 *
 * Mirrors apps/shared/set_target/core.py line for line; both run
 * apps/shared/set_target/parity_cases.json (tests/unit/set-target.test.mjs and
 * tests/shared/test_set_target_core.py), so the header readout and the
 * HTTP/CLI/MCP summary cannot drift.
 *
 * The per-track estimate is estimateTrack from ./played-length (LIBUX-8673);
 * this module only sums it per set.
 */

import { estimateTrack } from './played-length';

const CAMELOT_RE = /^\s*(1[0-2]|[1-9])\s*([AB])\s*$/i;

export interface SetEntry {
	stable_id: string;
	duration_s: number | null;
	bpm: number | null;
	key: string | null;
	skipped?: boolean;
	mix_in_s?: number | null;
	mix_out_s?: number | null;
}

export interface KeyClash {
	from_id: string;
	to_id: string;
	from_key: string;
	to_key: string;
	steps: number;
	gross: boolean;
}

export interface SetSummary {
	played_s: number;
	target_s: number | null;
	over_s: number | null;
	skipped: number;
	clashes: KeyClash[];
}

/** Estimated seconds a track is heard in a mix (the shared LIBUX-8673 rule); 0 without a duration. */
export function playedLengthS(
	durationS: number | null,
	bpm: number | null,
	mixInS: number | null = null,
	mixOutS: number | null = null
): number {
	return estimateTrack({ duration_s: durationS, bpm, mix_in_s: mixInS, mix_out_s: mixOutS })[0];
}

export function parseCamelot(key: string | null | undefined): [number, string] | null {
	if (!key) return null;
	const match = CAMELOT_RE.exec(key);
	return match === null ? null : [Number(match[1]), match[2].toUpperCase()];
}

export function wheelSteps(a: number, b: number): number {
	const diff = Math.abs(a - b);
	return Math.min(diff, 12 - diff);
}

/** [steps, gross] when the pair clashes; null when compatible or a key is unknown. */
export function keyClash(fromKey: string | null, toKey: string | null): [number, boolean] | null {
	const left = parseCamelot(fromKey);
	const right = parseCamelot(toKey);
	if (left === null || right === null) return null;
	const steps = wheelSteps(left[0], right[0]);
	const letterChange = left[1] !== right[1];
	if (steps === 0) return null;
	else if (steps === 1 && !letterChange) return null;
	return [steps, steps >= 3 || (letterChange && steps >= 2)];
}

/** Played length of the non-skipped tracks, target delta and every adjacent key clash. */
export function summarizeSet(entries: readonly SetEntry[], targetS: number | null): SetSummary {
	const live = entries.filter((e) => e.skipped !== true);
	const played = live.reduce(
		(sum, e) => sum + playedLengthS(e.duration_s, e.bpm, e.mix_in_s ?? null, e.mix_out_s ?? null),
		0
	);
	const clashes: KeyClash[] = [];
	for (let i = 0; i + 1 < live.length; i += 1) {
		const [left, right] = [live[i], live[i + 1]];
		const verdict = keyClash(left.key, right.key);
		if (verdict === null) continue;
		clashes.push({
			from_id: left.stable_id,
			to_id: right.stable_id,
			from_key: String(left.key).trim().toUpperCase(),
			to_key: String(right.key).trim().toUpperCase(),
			steps: verdict[0],
			gross: verdict[1]
		});
	}
	return {
		played_s: played,
		target_s: targetS,
		over_s: targetS === null ? null : played - targetS,
		skipped: entries.length - live.length,
		clashes
	};
}

/** Python round() is banker's rounding; mirror it so x.5 minutes agree. */
function roundHalfEven(x: number): number {
	const floor = Math.floor(x);
	const diff = x - floor;
	if (diff > 0.5) return floor + 1;
	else if (diff < 0.5) return floor;
	return floor % 2 === 0 ? floor : floor + 1;
}

/** '~48m of 60m · 12m left' / '~67m of 60m · 7m over' / '~48m' without a target. */
export function formatReadout(playedS: number, targetS: number | null): string {
	const playedM = roundHalfEven(playedS / 60);
	if (targetS === null) return `~${playedM}m`;
	const targetM = roundHalfEven(targetS / 60);
	const delta = playedM - targetM;
	return delta > 0
		? `~${playedM}m of ${targetM}m · ${delta}m over`
		: `~${playedM}m of ${targetM}m · ${-delta}m left`;
}
