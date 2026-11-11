/** Fixture MIK energy block for Storybook contracts (issue #10132). Synthesized, not a recorded payload. */

import type { TryCandidate, TryEnergyBlock } from './tryer-model';

export const TRYER_ENERGY_FIXTURE: TryEnergyBlock = {
	line: 'energy 6 -> 7 (MIK track levels)   seam 5 -> 7 at in bar 57   intent energy_up wants +1..+2: fits',
	numbers: [
		{ value: '6', title: 'MIK scalar on the outgoing track (track_fields.energy)' },
		{ value: '7', title: 'MIK scalar on the incoming track (track_fields.energy)' },
		{ value: '5', title: 'MIK level at outgoing bar 41 (last overlapping bar)' },
		{ value: '7', title: 'MIK level at incoming bar 57 (landing bar)' },
		{ value: '57', title: 'Incoming landing bar on the beatgrid' }
	],
	unscored: false,
	mik_grid_suspect: false,
	grid_warning: null,
	out: { start_bar: 26, levels: [6, 6, 6, 6, 6, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5] },
	in: { start_bar: 41, levels: [5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 6, 6, 7, 7, 7] },
	overlap_out_start_bar: 26,
	overlap_out_end_bar: 41,
	overlap_in_start_bar: 41,
	overlap_in_end_bar: 57,
	boundaries: [
		{ bar: 33, deck: 'out' },
		{ bar: 49, deck: 'in' }
	]
};

/** Minimal try candidate for Storybook; only energy is exercised by TryerEnergyCard. */
export function tryerEnergyChip(energy: TryEnergyBlock): TryCandidate {
	return {
		bars: 16,
		energy,
		factors: {},
		id: 'c1',
		measured: { k: 0, n: 0 },
		metrics: {},
		plan: {},
		plan_sha256: 'sha-energy-fixture',
		rank_group: 'measured',
		refused: null,
		render_ms: 0,
		render_url: '/api/v1/dj/try/j/candidates/c1/audio',
		score: 0.7,
		score_ms: 0,
		scorer_version: '0.1.0',
		style: 'blend',
		unknown: {},
		vetoes: []
	};
}
