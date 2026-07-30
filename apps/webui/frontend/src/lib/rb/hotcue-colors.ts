// Rekordbox ColorTableIndex palette (0x00..0x3e) tuned for our dark UI.
// Index 0 / null = default green (legacy CDJ hot-cue). Adjust shades here;
// theme.css exposes --rb-hotcue-default / --rb-hotcue-loop-default fallbacks.
// Source indices match Pioneer extended ANLZ / rekordcrate hot-cue color codes.

/** Default filled cue accent when ColorTableIndex is null or 0. */
export const HOTCUE_DEFAULT = '#35c04f';

/** Default loop accent when ColorTableIndex is null or 0. */
export const HOTCUE_LOOP_DEFAULT = '#e8a13a';

/**
 * ColorTableIndex -> CSS hex. Sparse unknown indices fall back via
 * {@link hotCueColorCss}. Values are slightly softened RB display colors
 * so they read on --rb-panel-raised without neon blowout.
 */
export const HOTCUE_COLOR_TABLE: Readonly<Record<number, string>> = {
	0: HOTCUE_DEFAULT,
	1: '#4a6ef0',
	2: '#5a7af0',
	3: '#5a8cf0',
	4: '#5a9ef0',
	5: '#5ab0f0',
	6: '#5aace8',
	7: '#5aaadb',
	8: '#4ea8d0',
	9: '#20c8e8',
	10: '#28c4dc',
	11: '#3abcc8',
	12: '#2aa8a8',
	13: '#28a098',
	14: '#289888',
	15: '#249880',
	16: '#209c78',
	17: '#20a070',
	18: '#1ca868',
	19: '#38c068',
	20: '#40cc58',
	21: '#48d850',
	22: '#38d028',
	23: '#74b048',
	24: '#80b838',
	25: '#8cc430',
	26: '#98cc24',
	27: '#98c818',
	28: '#9cbc18',
	29: '#a4b414',
	30: '#a4ac14',
	31: '#a8a414',
	32: '#b0a014',
	33: '#d09c10',
	34: '#e89810',
	35: '#e89010',
	36: '#e88810',
	37: '#e87410',
	38: '#c86028',
	39: '#c84828',
	40: '#c83828',
	41: '#c83028',
	42: '#d03838',
	43: '#e84868',
	44: '#e84068',
	45: '#e82870',
	46: '#e03080',
	47: '#d83890',
	48: '#d040a0',
	49: '#c848b8',
	50: '#c84888',
	51: '#d038a0',
	52: '#d028c0',
	53: '#d010e0',
	54: '#c810e0',
	55: '#b810e0',
	56: '#a038e0',
	57: '#a840e0',
	58: '#b048e0',
	59: '#9858e0',
	60: '#9868e0',
	61: '#7868e0',
	62: '#6070e0'
};

/** Resolve ColorTableIndex to a CSS color; loops use orange default. */
export function hotCueColorCss(
	colorTableIndex: number | null | undefined,
	isLoop = false
): string {
	if (colorTableIndex === null || colorTableIndex === undefined || colorTableIndex === 0) {
		return isLoop ? HOTCUE_LOOP_DEFAULT : HOTCUE_DEFAULT;
	}
	return HOTCUE_COLOR_TABLE[colorTableIndex] ?? (isLoop ? HOTCUE_LOOP_DEFAULT : HOTCUE_DEFAULT);
}
