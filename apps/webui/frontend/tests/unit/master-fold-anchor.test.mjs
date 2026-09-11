/**
 * Where the "▲ MASTER" jump button sits over the track table.
 *
 * Pin b44c957f082f (the maintainer, Wed 2 Sep 2026): "jump to master button should be
 * aligned with track title col. lazy updates when window resized to minimize
 * rendering cost (requirement)."
 *
 * It used to be `left: 50%` - the middle of the table, which lands over the
 * Rating / Comments columns and points at nothing. The title column is what
 * the badge is about, so it is what it should sit over.
 *
 * The second half of the pin is answered by construction rather than by a
 * throttle: the offset is a sum over colWidths, which is state the table
 * already owns, plus the wrap's existing scrollLeft. Nothing here measures the
 * window, so a resize recomputes nothing and there is no listener to debounce.
 * That is cheaper than a lazy update, and it cannot drift out of sync with one.
 *
 * Regression lines:
 * - if the offset stops tracking the columns before the title then the badge
 *   points at whatever column happens to be in the middle
 * - if it stops subtracting scrollLeft then it detaches from the column the
 *   moment the table is scrolled sideways
 * - if the clamp goes then a narrow panel can park the badge off its own left
 *   edge where it cannot be clicked
 * - if it starts reading the window then the resize cost the pin asked to
 *   avoid is back
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/master-fold-anchor.ts');
});

/** The shipped defaults, so the numbers below are the real ones. */
const WIDTHS = {
	funnel: 24,
	err: 24,
	cloud: 24,
	order: 34,
	preview: 177,
	art: 54,
	title: 220
};
const BEFORE_TITLE = 24 + 24 + 24 + 34 + 177 + 54; // 337

describe('masterFoldCenterPx', () => {
	it('centres on the title column, not the table', () => {
		const { masterFoldCenterPx } = mod;
		assert.equal(masterFoldCenterPx(WIDTHS, 0), BEFORE_TITLE + 220 / 2); // 447
	});

	it('follows a column resize with no measurement', () => {
		const { masterFoldCenterPx } = mod;
		// Widen Preview by 60: the title column, and the badge, both move right.
		const wider = { ...WIDTHS, preview: 237 };
		assert.equal(masterFoldCenterPx(wider, 0), BEFORE_TITLE + 60 + 110);
		// Widen Title itself: the centre moves by half the change.
		assert.equal(masterFoldCenterPx({ ...WIDTHS, title: 320 }, 0), BEFORE_TITLE + 160);
	});

	it('stays over the column when the table is scrolled sideways', () => {
		const { masterFoldCenterPx } = mod;
		assert.equal(masterFoldCenterPx(WIDTHS, 100), 347);
	});

	it('never parks off the left edge where it cannot be clicked', () => {
		const { masterFoldCenterPx, MASTER_FOLD_MIN_LEFT_PX } = mod;
		assert.ok(MASTER_FOLD_MIN_LEFT_PX > 0);
		// Scrolled far enough that the title column is off-screen left.
		assert.equal(masterFoldCenterPx(WIDTHS, 5000), MASTER_FOLD_MIN_LEFT_PX);
	});

	it('never parks off the right edge of a wrap narrower than the columns', () => {
		const { masterFoldCenterPx, MASTER_FOLD_MIN_LEFT_PX } = mod;
		// BEFORE_TITLE + 220/2 = 447 would sit past a 300px-wide wrap.
		assert.equal(masterFoldCenterPx(WIDTHS, 0, 300), 300 - MASTER_FOLD_MIN_LEFT_PX);
	});

	it('skips the max clamp when no wrap width is given', () => {
		const { masterFoldCenterPx } = mod;
		assert.equal(masterFoldCenterPx(WIDTHS, 0), BEFORE_TITLE + 220 / 2);
	});

	it('clamps the left edge past the min, not just the centre, once translateX(-50%) is accounted for (Sol review r3941617671)', () => {
		const { masterFoldCenterPx, MASTER_FOLD_MIN_LEFT_PX } = mod;
		// Scrolled far left, an 80px-wide badge (40px half-width): the centre
		// alone would clamp at MIN_LEFT_PX, but translateX(-50%) would still
		// push the badge's own left edge 40px further off-screen than that.
		assert.equal(
			masterFoldCenterPx(WIDTHS, 5000, undefined, 40),
			MASTER_FOLD_MIN_LEFT_PX + 40
		);
	});

	it('clamps the right edge past the wrap width, not just the centre, once translateX(-50%) is accounted for (Sol review r3941617671)', () => {
		const { masterFoldCenterPx, MASTER_FOLD_MIN_LEFT_PX } = mod;
		// Same narrow-wrap case as above, now with the badge's own half-width
		// pulled in from the right edge too.
		assert.equal(
			masterFoldCenterPx(WIDTHS, 0, 300, 40),
			300 - MASTER_FOLD_MIN_LEFT_PX - 40
		);
	});

	it('defaults badgeHalfWidthPx to 0, leaving every pre-fix caller and its numbers unchanged', () => {
		const { masterFoldCenterPx, MASTER_FOLD_MIN_LEFT_PX } = mod;
		assert.equal(masterFoldCenterPx(WIDTHS, 5000), MASTER_FOLD_MIN_LEFT_PX);
		assert.equal(masterFoldCenterPx(WIDTHS, 0, 300), 300 - MASTER_FOLD_MIN_LEFT_PX);
	});

	it('is arithmetic over state, and reads nothing from the window', () => {
		const source = readFileSync(
			fileURLToPath(new URL('../../src/lib/rb/master-fold-anchor.ts', import.meta.url)),
			'utf8'
		);
		// Comments talk ABOUT the window, so strip them and check the code.
		const code = source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
		for (const forbidden of [
			'window.',
			'globalThis',
			'ResizeObserver',
			'getBoundingClientRect',
			'document.'
		]) {
			assert.equal(
				code.includes(forbidden),
				false,
				`master-fold-anchor reads ${forbidden}; the pin asked for no resize cost`
			);
		}
	});
});
