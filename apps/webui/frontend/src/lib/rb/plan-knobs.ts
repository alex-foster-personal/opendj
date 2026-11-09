/** WAVEPLAN-7896: the knobs a plan's curves draw, in drawing order, and each one's line. Apart from plan-overlay.ts so the
 *  waveform row reaches them without pulling the geometry builder into /performance's first load. */
export type PlanKnob = 'fader' | 'eq_low' | 'eq_mid' | 'eq_high' | 'filter';

export type PlanKnobColor = 'planFader' | 'planEqLow' | 'planEqMid' | 'planEqHigh' | 'planFilter';

export const PLAN_KNOBS: readonly { name: PlanKnob; dash: number[]; color: PlanKnobColor }[] = [
	{ name: 'fader', dash: [], color: 'planFader' },
	{ name: 'eq_low', dash: [6, 3], color: 'planEqLow' },
	{ name: 'eq_mid', dash: [6, 3], color: 'planEqMid' },
	{ name: 'eq_high', dash: [6, 3], color: 'planEqHigh' },
	{ name: 'filter', dash: [2, 3], color: 'planFilter' }
];
