/**
 * The /anlz vocals contract (shared contract point 5): the wire types and
 * their validators. Pure, with no backend access, so presentational
 * paint paths (wave/render, waveform previews) can use it without pulling
 * in api-rb. api-rb re-exports everything here.
 */

import type { AnlzData } from './anlz-types';

/** One vocal region. For 'rekordbox' intensity is the max PVDI value
 * (1..4) within the run; for 'demucs' it is the worker confidence
 * mapped onto the same 1..4 ramp, so both render identically. */
export interface VocalRegion {
	start_s: number;
	end_s: number;
	intensity: number;
}

export type Vocals =
	| { status: 'rekordbox'; fps: number; regions: VocalRegion[] }
	| { status: 'no_vocals'; fps: number; regions: VocalRegion[] } // regions always []
	| { status: 'demucs'; fps: number; regions: VocalRegion[] } // local detection; [] = none found
	| { status: 'not_analyzed' };

/** AnlzData plus the contract's vocals field. types.ts is the frozen
 * build-unit contract, so the widening lives here (api-rb owns types). */
export type AnlzWithVocals = AnlzData & { vocals: Vocals };

const _vocalsMemo = new WeakMap<AnlzData, Vocals>();

/** Validate a wire ``vocals`` object (listing rows or /anlz). Throws on
 * absent/malformed: that is a contract breach, never invent regions. */
export function parseVocals(raw: unknown): Vocals {
	if (typeof raw !== 'object' || raw === null) {
		throw new Error(
			'payload has no valid "vocals" field - backend contract point 5 not met'
		);
	}
	const v = raw as { status?: unknown; fps?: unknown; regions?: unknown };
	if (v.status === 'not_analyzed') return { status: 'not_analyzed' };
	if (v.status !== 'rekordbox' && v.status !== 'no_vocals' && v.status !== 'demucs') {
		throw new Error(`anlz vocals: unknown status ${JSON.stringify(v.status)}`);
	}
	if (typeof v.fps !== 'number') throw new Error('anlz vocals: fps missing or not a number');
	if (!Array.isArray(v.regions)) throw new Error('anlz vocals: regions missing or not an array');
	const regions = v.regions.map((r: unknown, i: number): VocalRegion => {
		const rr = r as { start_s?: unknown; end_s?: unknown; intensity?: unknown };
		if (
			typeof rr.start_s !== 'number' ||
			typeof rr.end_s !== 'number' ||
			typeof rr.intensity !== 'number'
		) {
			throw new Error(`anlz vocals: region ${i} malformed (need start_s/end_s/intensity numbers)`);
		}
		return { start_s: rr.start_s, end_s: rr.end_s, intensity: rr.intensity };
	});
	if (v.status === 'no_vocals' && regions.length !== 0) {
		throw new Error('anlz vocals: status no_vocals but regions non-empty - contract violation');
	}
	if (v.status === 'rekordbox' && regions.length === 0) {
		throw new Error(
			'anlz vocals: status rekordbox with zero regions - the contract calls that no_vocals'
		);
	}
	return { status: v.status, fps: v.fps, regions };
}

/** Validated vocals of an anlz payload (memoized per payload object -
 * safe to call from rAF paint paths). Throws loudly when the field is
 * absent/malformed: that is a contract breach, NOT one of the three UI
 * states, and must never be rendered as "no vocals". */
export function vocalsOf(anlz: AnlzData): Vocals {
	const memo = _vocalsMemo.get(anlz);
	if (memo !== undefined) return memo;
	const vocals = parseVocals((anlz as AnlzData & { vocals?: unknown }).vocals);
	_vocalsMemo.set(anlz, vocals);
	return vocals;
}
