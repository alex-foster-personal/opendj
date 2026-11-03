/** WAVEPLAN-7896: the knobs a plan's curves draw, in drawing order, and each one's line. Apart from plan-overlay.ts so the
 *  waveform row reaches them without pulling the geometry builder into /performance's first load. */
export type PlanKnob = 'fader' | 'eq_low' | 'eq_mid' | 'eq_high' | 'filter';

export const PLAN_KNOBS: readonly { name: PlanKnob; dash: number[] }[] = [
	{ name: 'fader', dash: [] },
	{ name: 'eq_low', dash: [6, 3] },
	{ name: 'eq_mid', dash: [6, 3] },
	{ name: 'eq_high', dash: [6, 3] },
	{ name: 'filter', dash: [2, 3] }
];
