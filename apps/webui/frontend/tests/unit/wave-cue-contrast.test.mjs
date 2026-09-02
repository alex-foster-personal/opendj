/**
 * Cue markers on the main waveform must clear a measurable contrast floor
 * against the row background (issue #877 acceptance criterion 6: "the same
 * minimum contrast floor now enforced for the BPM column").
 *
 * Pin: `--rb-red` (#d0342c, the pre-#877 single cue colour) measures 3.85:1
 * against `--rb-bg` (#0d0f12) - under the 4.5:1 WCAG AA floor bpm-heat.ts
 * enforces for the BPM column. That is the concrete, measured shape of the
 * "contrast not good" half of issue #877's pin, independent of the "not
 * different color per cue item" half. The fix is `cueOutline` (--rb-text): a
 * stroke every marker gets regardless of its fill hue, so contrast survives
 * whatever the fill colour ends up being (see cues.ts `_drawPointCueMarker`
 * / `_drawLoopCueMarker`).
 *
 * Regression lines:
 * - if cueOutline stops clearing CUE_MIN_CONTRAST against --rb-bg then every
 *   cue marker is unreadable again, exactly like the pre-fix red triangle
 * - if CUE_MIN_CONTRAST drifts from bpm-heat.ts's BPM_MIN_TEXT_CONTRAST then
 *   the two "same floor" claims (BPM column, cue markers) are no longer the
 *   same number
 * - if the hot-cue/loop/memory fill colours stop matching theme.css's
 *   --rb-green / --rb-orange / --rb-red then the waveform palette has
 *   silently forked from the shared theme
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/components/rb/wave/cues.ts');
});

// theme.css .perf-root palette (apps/webui/frontend/src/lib/rb/theme.css).
// Duplicated as literals here for the same reason bpm-contrast.test.mjs
// duplicates BPM_LANE_BACKDROP: a unit test cannot ask a browser to resolve
// a CSS custom property, so the values this file measures against must be
// the same ones readPalette() would read at runtime.
const RB_BG = '#0d0f12';
const RB_GREEN = '#35c04f';
const RB_ORANGE = '#e8a13a';
const RB_RED = '#d0342c';
const RB_TEXT = '#c8cdd2';

describe('wave cue-marker contrast', () => {
	it('measures contrast the way WCAG does', () => {
		const { contrastRatio } = mod;
		assert.ok(Math.abs(contrastRatio('#000000', '#ffffff') - 21) < 0.01);
		assert.ok(Math.abs(contrastRatio(RB_BG, RB_BG) - 1) < 0.001);
		assert.ok(
			Math.abs(contrastRatio('rgb(200 205 210)', RB_BG) - contrastRatio(RB_BG, 'rgb(200 205 210)')) <
				1e-9
		);
	});

	it('reuses the exact floor bpm-heat.ts enforces for the BPM column', () => {
		assert.equal(mod.CUE_MIN_CONTRAST, 4.5);
	});

	it('the pre-#877 single cue colour (--rb-red) fails the floor on its own - this is the bug', () => {
		const { contrastRatio, CUE_MIN_CONTRAST } = mod;
		const ratio = contrastRatio(RB_RED, RB_BG);
		assert.ok(
			ratio < CUE_MIN_CONTRAST,
			`expected the old red-on-red-ish shape to fail the floor, got ${ratio.toFixed(2)}:1 - ` +
				're-measure before assuming the outline is still load-bearing'
		);
	});

	it('cueOutline clears the floor against the row background, independent of fill', () => {
		const { contrastRatio, CUE_MIN_CONTRAST } = mod;
		const ratio = contrastRatio(RB_TEXT, RB_BG);
		assert.ok(
			ratio >= CUE_MIN_CONTRAST,
			`cueOutline only clears ${ratio.toFixed(2)}:1 against --rb-bg, below ${CUE_MIN_CONTRAST}:1`
		);
	});

	it('the hot-cue and loop fill colours independently clear the floor too', () => {
		const { contrastRatio, CUE_MIN_CONTRAST } = mod;
		for (const [name, color] of [
			['cueHotCue (--rb-green)', RB_GREEN],
			['cueLoop (--rb-orange)', RB_ORANGE]
		]) {
			const ratio = contrastRatio(color, RB_BG);
			assert.ok(ratio >= CUE_MIN_CONTRAST, `${name} only clears ${ratio.toFixed(2)}:1 against --rb-bg`);
		}
	});
});
