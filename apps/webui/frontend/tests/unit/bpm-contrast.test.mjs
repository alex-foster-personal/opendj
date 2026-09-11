/**
 * The BPM cell must stay READABLE at every point of its heat ramp.
 *
 * Pin 7c0c0167cb0a (the maintainer, Wed 2 Sep 2026): "sometimes bpm is almost
 * unreadable with eg gray insuficient contrast to the green/yellow/bg color -
 * add requirement for minimum readability contrast, should be some way of
 * enumerating this into unit tests that cover various state scenarios."
 *
 * This file is that enumeration. The ramp used to run to #1a1c20 (near black)
 * over a #14171d row - a contrast ratio of 1.06:1, which is not dim, it is
 * invisible. The lane tints made it worse in a second way the eye notices
 * first: the sweet lane paints green behind the text and the half lane paints
 * purple, so "the number I cannot read" and "the number on a coloured cell"
 * were the same rows.
 *
 * The contract is WCAG 2.1 AA for body text (4.5:1) against the backdrop the
 * cell actually composites onto, per lane. Ordering across the ramp is
 * preserved: closer BPMs stay brighter than distant ones, right up to the
 * point where the floor takes over.
 *
 * Regression lines:
 * - if any lane's dimmest BPM colour drops under 4.5:1 on its own backdrop
 *   then the cell is unreadable again
 * - if the ramp stops being monotonic then "brighter = closer to master" has
 *   stopped being true and the colour says nothing
 * - if contrastRatio stops matching the WCAG reference pairs then the floor
 *   is being measured with the wrong ruler
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';
import {
	BPM_LANE_BACKDROP,
	contrastRatio,
	parseColorMixBackdrop,
	readCssVarHex,
	relativeLuminance,
	WCAG_AA_BODY_TEXT
} from './contrast.mjs';

/** `classifyBpmHeat` returns a CSS string for the DOM; the contrast helpers
 * take triples. Reading it back here is deliberate: it proves the string the
 * browser actually receives clears the floor, not an intermediate value. */
function rgbOf(css) {
	const m = /^rgb\((\d+) (\d+) (\d+)\)$/.exec(css);
	assert.ok(m !== null, `unreadable colour from the ramp: ${css}`);
	return { r: Number(m[1]), g: Number(m[2]), b: Number(m[3]) };
}

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/bpm-heat.ts');
});

/** Every BPM a row can hold against a master of 128, coarse enough to run
 * fast and fine enough to walk the whole ramp including both folds. */
const MASTER = 128;
const SWEEP = [];
for (let bpm = 40; bpm <= 300; bpm += 1) SWEEP.push(bpm);

describe('bpm-heat contrast', () => {
	it('measures contrast the way WCAG does', () => {
		const black = { r: 0, g: 0, b: 0 };
		const white = { r: 255, g: 255, b: 255 };
		const panel = { r: 20, g: 23, b: 29 };
		// The two anchor pairs from the spec: black on white is 21:1, and any
		// colour against itself is 1:1.
		assert.ok(Math.abs(contrastRatio(black, white) - 21) < 0.01);
		assert.ok(Math.abs(contrastRatio(panel, panel) - 1) < 0.001);
		// Symmetric.
		assert.ok(Math.abs(contrastRatio(white, panel) - contrastRatio(panel, white)) < 1e-9);
	});

	it('derives the sweet/half backdrops from the shipped CSS, not a hand copy', () => {
		// Positive control: the composited value tracks a rendered color-mix()
		// (base 82%/tint 18% for sweet, matching TrackTable.svelte today).
		const panel = readCssVarHex('--rb-panel: #14171d;', 'rb-panel');
		const composited = parseColorMixBackdrop(
			'.c-bpm.bpm-sweet { background: color-mix(in srgb, var(--rb-green) 18%, transparent); }',
			'--rb-green: #35c04f;',
			'sweet',
			panel
		);
		assert.deepEqual(composited, BPM_LANE_BACKDROP.sweet);
	});

	it('throws rather than silently matching an absent selector or variable', () => {
		// Negative control, per .claude/rules/verification.md: a parse that
		// finds nothing must report UNKNOWN (throw), never compute NaN and
		// let the sweep below pass against a fiction.
		assert.throws(() => readCssVarHex('/* no vars here */', 'rb-panel'));
		assert.throws(() =>
			parseColorMixBackdrop('.c-bpm.bpm-mid { background: none; }', '', 'sweet', { r: 0, g: 0, b: 0 })
		);
	});

	it('names a backdrop for every lane', () => {
		
		for (const lane of ['sweet', 'half', 'mid', 'far']) {
			const b = BPM_LANE_BACKDROP[lane];
			assert.ok(
				b !== undefined && [b.r, b.g, b.b].every(Number.isFinite),
				`lane ${lane} has no backdrop, so its contrast is unmeasurable`
			);
		}
	});

	it('clears 4.5:1 for every BPM in the sweep, on its own lane backdrop', () => {
		const { classifyBpmHeat } = mod;
		assert.equal(WCAG_AA_BODY_TEXT, 4.5);
		const offenders = [];
		for (const bpm of SWEEP) {
			const heat = classifyBpmHeat(bpm, MASTER);
			const ratio = contrastRatio(rgbOf(heat.color), BPM_LANE_BACKDROP[heat.lane]);
			if (ratio < WCAG_AA_BODY_TEXT - 1e-9) {
				offenders.push(`${bpm} (${heat.lane}) ${heat.color} = ${ratio.toFixed(2)}:1`);
			}
		}
		assert.deepEqual(offenders, [], `unreadable BPM cells: ${offenders.slice(0, 6).join('; ')}`);
	});

	it('clears the floor for a master of 90 and of 174 too', () => {
		const { classifyBpmHeat } = mod;
		for (const master of [90, 174]) {
			for (const bpm of SWEEP) {
				const heat = classifyBpmHeat(bpm, master);
				const ratio = contrastRatio(rgbOf(heat.color), BPM_LANE_BACKDROP[heat.lane]);
				assert.ok(
					ratio >= WCAG_AA_BODY_TEXT - 1e-9,
					`master ${master} bpm ${bpm} (${heat.lane}): ${ratio.toFixed(2)}:1`
				);
			}
		}
	});

	it('keeps the ramp meaningful: closer is never darker', () => {
		const { classifyBpmHeat } = mod;
		// Walking away from the master must never get BRIGHTER: the colour
		// carries the distance, and a floor that inverted it would be worse
		// than the unreadable version it replaced.
		let previous = Infinity;
		for (let bpm = MASTER; bpm <= MASTER * 1.09; bpm += 0.25) {
			const lum = relativeLuminance(rgbOf(classifyBpmHeat(bpm, MASTER).color));
			assert.ok(lum <= previous + 1e-9, `luminance rose at ${bpm} BPM`);
			previous = lum;
		}
		// And it must still have somewhere to go: an exact match has to be
		// visibly brighter than a 10% detune, or the floor ate the ramp.
		const exact = relativeLuminance(rgbOf(classifyBpmHeat(MASTER, MASTER).color));
		const far = relativeLuminance(rgbOf(classifyBpmHeat(MASTER * 1.1, MASTER).color));
		assert.ok(exact > far * 1.5, `ramp collapsed: ${exact} vs ${far}`);
	});

	it('still returns null when there is no master or no BPM', () => {
		const { classifyBpmHeat, bpmHeatColor } = mod;
		assert.equal(classifyBpmHeat(128, null), null);
		assert.equal(classifyBpmHeat(null, 128), null);
		assert.equal(bpmHeatColor(128, 0), null);
	});
});
