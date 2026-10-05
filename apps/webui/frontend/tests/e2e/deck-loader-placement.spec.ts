/**
 * The deck loader must appear above a selected, hovered track row and leave
 * the track's own line available for double-clicking.
 *
 * The regression is a HIT-TEST fact, not a styling opinion, so this
 * spec measures the RENDERED layout in a real browser rather than the source
 * (`tests/unit/quick-load-deck-box.test.mjs` and `deck-picker-corridor.test.mjs`
 * read the CSS text; neither can see where a box actually lands). While the
 * quick-load box was `top: 0; height: 100%` inside `.c-title` it covered the
 * right-hand side of the track's own line, and every target in it calls
 * `stopPropagation()` on `dblclick` (TrackTable.svelte) - so a double-click
 * aimed at the row landed on a button and the row's own load-and-play never
 * fired. Moving the box above the row is what frees that line again.
 *
 * WHY THIS FILE LIVES IN THE ROOT SUITE: it needs a real backend with a real
 * (generated, throwaway) library, because PREFLIGHT-01 holds the whole app on
 * its "Starting up" screen until one is attached, so /performance cannot mount
 * without it. The root config already builds exactly that fixture and IS run
 * per PR (e2e.yml, "Root Playwright suite"). The `performance-*` configs need
 * the declared analyzed-library inputs and are wired into no workflow at all,
 * so this regression belongs in the root suite that runs per PR.
 *
 * Requirements (this file is the mini-PRD for the placement contract):
 *
 * - ✔︎ ✅ 🎯 While a row is selected AND hovered, the box's rendered rect lies
 *   entirely above that row's own rect.
 * - ✔︎ ✅ 🎯 No point along the row's own line hit-tests into the box, so
 *   nothing there can swallow the row's double-click.
 * - ✔︎ ✅ 🎯 The box is really usable where it now sits: every deck target's own
 *   centre resolves to itself, so it is neither clipped away by the cell (a
 *   `td` is `overflow: hidden` for title truncation) nor covered by the row
 *   above it.
 * - ✔︎ ✅ 🎯 Reveal is still gated on selected AND hovered: hovering the title
 *   of an unselected row leaves that row's box inert.
 * - ✔︎ ✅ 🎯 The ROW ABOVE stays an ordinary row while its neighbour's box hangs
 *   over it (Sol review thread 3961773278): only the box's buttons may take a
 *   pointer there, they keep to the right of that cell's midpoint, and the box
 *   is no taller than a row.
 * - ✔︎ ✅ 🎯 The FIRST row's box is neither clipped by the scroll container nor
 *   covered by the sticky header, which is all there is above row 0.
 *
 * Acceptance tests, "[if] <scenario> [then ⛔️]":
 * - [if] the box's rect overlaps its own row's vertical span [then ⛔️]
 * - [if] any sampled point along the row's line resolves inside the box [then ⛔️]
 * - [if] a deck target's own centre point does not resolve to that target,
 *   i.e. it is clipped or covered where it now sits [then ⛔️]
 * - [if] hovering an unselected row's title makes its box hittable [then ⛔️]
 * - [if] any point on the row ABOVE resolves to the box's padding, border,
 *   background or label, or its buttons cross that cell's midpoint, or that
 *   cell's centre resolves into the box, or Playwright cannot hover it
 *   [then ⛔️]
 * - [if] a button of the FIRST row's box does not resolve to itself, i.e. the
 *   header or the container clip took it [then ⛔️]
 */
import { expect, test, type Page } from '@playwright/test';

const TRACK_ROW = '[data-testid="track-row"]';
/**
 * 1000px tall, NOT the suite's default 720: below ~969px only ONE of the two
 * competing vertical floors fits (497px deck area + 272px library + 200px
 * topbar/wave - see performance-library-min-rows.spec.ts), the deck area wins,
 * and its overflow then lies over the library's top strip and takes the
 * hit-testing there with it. Measured on this fixture at 1280x720, with the
 * deck box reverted or not: even `th.h-title`'s own centre resolves to the
 * deck's `DIV.main-row`, so the COLUMN HEADERS are unhittable at that height.
 * That is a pre-existing layout defect of its own, nothing to do with this
 * picker, and it would silently mask this spec's real subject. Test the
 * placement where the layout it depends on actually holds.
 */
const VIEWPORT = { width: 1280, height: 1000 };
// The fixture seeds two tracks. Row 1 is the one with a row above it, which is
// what proves the box is placed above AND is not clipped away up there.
const ROW_INDEX = 1;
// Issue #1558's DBLCLICK_GUARD_MS (TrackTable.svelte): the select-driven
// reveal delays button pointer-events by 500ms so a double-click's second
// click cannot land on a button. This spec's reachability checks assert
// steady-state usability (a user resting on the row, not mid double-click),
// so they must wait past the guard before hit-testing a target - otherwise
// they are asserting a target is clickable during the exact window it is
// deliberately inert. tests/e2e/quickload-dblclick-guard.spec.ts covers the
// guard window itself.
const PAST_DBLCLICK_GUARD_MS = 700;

async function _openAllTracks(page: Page): Promise<void> {
	await page.setViewportSize(VIEWPORT);
	// The enrich card (ENRICH-01) floats fixed bottom-right over the table and
	// shows on this fixture's unanalyzed library. Hidden the way the user hides
	// it (its own sessionStorage flag), so the hit tests below measure the row
	// and the sticky header, not an overlay this spec is not about.
	await page.addInitScript(() => sessionStorage.setItem('odj.enrich-card.hidden', '1'));
	await page.goto('/performance');
	await expect(page.locator('[data-testid="track-table"]')).toBeVisible({ timeout: 60_000 });
	await page.getByText('All Tracks', { exact: true }).first().click();
	// Wait for the row under test itself, not merely "some row": the fixture
	// seeds exactly two tracks, and on a contended machine the second one can
	// land seconds after the first.
	await expect(page.locator(TRACK_ROW).nth(ROW_INDEX)).toBeVisible({ timeout: 60_000 });
}

/**
 * Select + hover a row exactly the way a mouse user does: click its title cell
 * (which carries no handler of its own and selects purely by bubbling to the
 * row), then hover that same cell - one of the box's two reveal triggers. The
 * selection is ASSERTED, never assumed: while hidden the box is still laid out,
 * so a missing `rb-row-selected` otherwise presents as an inexplicable timeout.
 */
async function _selectAndHover(page: Page, index: number) {
	const row = page.locator(TRACK_ROW).nth(index);
	const title = row.locator('td.c-title');
	await title.click();
	await expect(row, 'the row did not select, so the quick-load box stays hidden').toHaveClass(
		/rb-row-selected/
	);
	await title.hover();
	return { row, title };
}

test('the deck loader floats above the track row and leaves the row line clickable', async ({
	page
}) => {
	test.setTimeout(120_000);
	await _openAllTracks(page);
	const { row } = await _selectAndHover(page, ROW_INDEX);
	await expect(row.locator('.deck-btns')).toBeVisible();
	await page.waitForTimeout(PAST_DBLCLICK_GUARD_MS);

	const geometry = await page.evaluate(() => {
		const selected = document.querySelector<HTMLElement>(
			'[data-testid="track-row"].rb-row-selected'
		);
		if (selected === null) throw new Error('no selected row');
		const box = selected.querySelector<HTMLElement>('.deck-btns');
		if (box === null) throw new Error('the selected row rendered no quick-load box');
		const rowRect = selected.getBoundingClientRect();
		const boxRect = box.getBoundingClientRect();

		// Every point along the row's own line, across its full width.
		const blockers: string[] = [];
		const y = rowRect.top + rowRect.height / 2;
		for (let x = rowRect.left + 2; x < rowRect.right - 2; x += 6) {
			const hit = document.elementFromPoint(x, y);
			if (hit !== null && box.contains(hit)) {
				blockers.push(`${Math.round(x)},${Math.round(y)} -> ${hit.tagName}.${hit.className}`);
			}
		}

		// ...and the box has to be genuinely usable where it now sits.
		const targets = [...box.querySelectorAll<HTMLElement>('button.deck-target')];
		const unreachable: string[] = [];
		for (const target of targets) {
			const rect = target.getBoundingClientRect();
			const hit = document.elementFromPoint(
				rect.left + rect.width / 2,
				rect.top + rect.height / 2
			);
			if (hit === null || !target.contains(hit)) {
				// Name what is painting over it, and the whole stack under that
				// point: "a DIV" is not enough to fix a stacking-context bug.
				const stack = document
					.elementsFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
					.slice(0, 5)
					.map(
						(element) =>
							`${element.tagName}.${element.className}` +
							`[z=${getComputedStyle(element).zIndex}]`
					)
					.join(' > ');
				unreachable.push(`${target.textContent?.trim()} -> ${stack}`);
			}
		}

		return {
			rowTop: rowRect.top,
			rowBottom: rowRect.bottom,
			boxTop: boxRect.top,
			boxBottom: boxRect.bottom,
			boxHeight: boxRect.height,
			blockers,
			targetCount: targets.length,
			unreachable
		};
	});

	expect(geometry.targetCount, 'the box must render one target per deck').toBe(4);
	expect(geometry.boxHeight, 'a zero-height box is above nothing').toBeGreaterThan(0);
	// 1px tolerance for subpixel layout rounding only.
	expect(
		geometry.boxBottom,
		`the box must sit ABOVE the row, not on its line: ${JSON.stringify(geometry)}`
	).toBeLessThanOrEqual(geometry.rowTop + 1);
	expect(
		geometry.blockers,
		'nothing on the row own line may hit-test into the quick-load box - that is what blocked the double-click'
	).toEqual([]);
	expect(
		geometry.unreachable,
		'every deck target must resolve to itself at its own centre, or the box is clipped/covered where it now sits'
	).toEqual([]);
});

test('the deck loader stays inert on a row that is hovered but not selected', async ({ page }) => {
	test.setTimeout(120_000);
	await _openAllTracks(page);
	const { row } = await _selectAndHover(page, ROW_INDEX);

	// Dismiss the active picker first (Sol review thread 3961773278). It hangs
	// over the row above, so leaving it up would make this test's subject -
	// the OTHER row's own box - depend on the selected row's box footprint.
	// Parking the pointer in the empty corner drops both reveal triggers.
	// (The box is never `display: none` - it stays laid out so Tab can reach
	// its buttons - so "dismissed" is opacity 0 plus inert buttons, not
	// `toBeHidden`.)
	await page.mouse.move(2, VIEWPORT.height - 2);
	await expect(row.locator('.deck-btns')).toHaveCSS('opacity', '0');
	await expect(row.locator('.deck-btns button').first()).toHaveCSS('pointer-events', 'none');

	const otherIndex = ROW_INDEX - 1;
	const other = page.locator(TRACK_ROW).nth(otherIndex);
	await other.locator('td.c-title').hover();
	await expect(other, 'hovering must not select a row on its own').not.toHaveClass(
		/rb-row-selected/
	);

	const hittable = await page.evaluate((index) => {
		const target = document.querySelectorAll<HTMLElement>('[data-testid="track-row"]')[index];
		if (target === undefined) throw new Error(`no row at index ${index}`);
		const box = target.querySelector<HTMLElement>('.deck-btns');
		if (box === null) throw new Error('that row rendered no quick-load box');
		const rect = box.getBoundingClientRect();
		const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
		return hit !== null && box.contains(hit);
	}, otherIndex);

	expect(hittable, 'an unselected row own quick-load box must stay inert under the pointer').toBe(
		false
	);
});

/**
 * The picker's failure mode, one row up. A review finding on this file
 * (review thread 3961773278) pointed out that a box hanging above row N lands
 * on row N-1's title, and the box was `pointer-events: auto` across its whole
 * painted area while revealed - so the fix for "the picker swallows its OWN
 * row's double-click" had re-created exactly that bug against the PREVIOUS
 * row. Measured on this fixture at head b771dcf02: 38 of the sampled points
 * along row 0's title line resolved into row 1's picker, 11 of them onto its
 * bare background/padding and its non-interactive "load to deck:" label.
 *
 * The row above a selected row is an ordinary row: its title must stay
 * hoverable and double-clickable. Only the picker's BUTTONS may take a pointer
 * there - they are the feature, and they sit at the cell's right edge, clear of
 * the title text and of the cell's own centre, which is where a click aimed at
 * that row lands. Everything else about the box must be transparent, which is
 * also what lets a pointer travelling upward pass through instead of latching
 * onto `.deck-btns:hover` and stranding the user one row short.
 */
test('a revealed picker does not intercept the row above it', async ({ page }) => {
	test.setTimeout(120_000);
	await _openAllTracks(page);
	const { row } = await _selectAndHover(page, ROW_INDEX);
	await expect(row.locator('.deck-btns')).toBeVisible();

	const previous = page.locator(TRACK_ROW).nth(ROW_INDEX - 1);
	const above = await page.evaluate((index) => {
		const rows = document.querySelectorAll<HTMLElement>('[data-testid="track-row"]');
		const selected = rows[index];
		const previousRow = rows[index - 1];
		if (selected === undefined || previousRow === undefined) {
			throw new Error('this fixture needs a row above the selected one');
		}
		const box = selected.querySelector<HTMLElement>('.deck-btns');
		if (box === null) throw new Error('the selected row rendered no quick-load box');
		const title = previousRow.querySelector<HTMLElement>('td.c-title');
		if (title === null) throw new Error('the row above rendered no title cell');
		const rect = title.getBoundingClientRect();
		const describe = (element: Element) => `${element.tagName}.${element.className}`;

		// Sample the row above's own title line, across its full width.
		const deadArea: string[] = [];
		let buttonPoints = 0;
		const y = rect.top + rect.height / 2;
		for (let x = rect.left + 2; x < rect.right - 2; x += 6) {
			const hit = document.elementFromPoint(x, y);
			if (hit === null || !box.contains(hit)) continue;
			if (hit.closest('button') !== null) buttonPoints += 1;
			else deadArea.push(`${Math.round(x)},${Math.round(y)} -> ${describe(hit)}`);
		}

		// ...and the point a click on that row actually uses.
		const centre = document.elementFromPoint(rect.left + rect.width / 2, y);
		const buttons = [...box.querySelectorAll<HTMLElement>('button')];
		if (buttons.length === 0) throw new Error('the box rendered no buttons');
		const clusterLeft = Math.min(...buttons.map((b) => b.getBoundingClientRect().left));
		const boxRect = box.getBoundingClientRect();
		return {
			deadArea,
			buttonPoints,
			centre: centre === null ? 'null' : describe(centre),
			centreIsInsideTheBox: centre !== null && box.contains(centre),
			clusterLeft,
			cellMidpoint: rect.left + rect.width / 2,
			cellWidth: rect.width,
			boxWidth: boxRect.width,
			boxHeight: boxRect.height,
			rowHeight: previousRow.getBoundingClientRect().height
		};
	}, ROW_INDEX);

	expect(
		above.deadArea,
		'the picker hangs over the row above, so only its BUTTONS may take the pointer there - its padding, border, background and label must not'
	).toEqual([]);
	expect(
		above.centreIsInsideTheBox,
		`the row above must still own its own title cell's centre, which is the point a click on it uses; it resolved to ${above.centre}`
	).toBe(false);

	expect(
		above.clusterLeft,
		`the hittable cluster must keep to the right-hand side of the row above's title cell, clear of its midpoint: ${JSON.stringify(above)}`
	).toBeGreaterThan(above.cellMidpoint);
	expect(
		above.boxHeight,
		`the box must be no taller than one row, or it reaches past its immediate neighbour: ${JSON.stringify(above)}`
	).toBeLessThanOrEqual(above.rowHeight);

	// And the real-user proof rather than a hit-test: Playwright refuses to
	// hover an element that would not receive the pointer at that point, so
	// this is the 120s hang Sol described, expressed as a 5s assertion.
	await previous.locator('td.c-title').hover({ timeout: 5_000 });
});

/**
 * The risk the row-1 tests cannot see: for the FIRST row there is no row above,
 * only the sticky column header and the top of the scroll container. A box
 * hanging upward from row 0 could be clipped away by the container's overflow
 * or painted over by the header, which would make its buttons unreachable
 * above the selected track row. Row 0 is also the row a user
 * meets first, so "it works from row 1 down" is not good enough.
 */
test('the picker on the FIRST row is not clipped or covered by the sticky header', async ({
	page
}) => {
	test.setTimeout(120_000);
	await _openAllTracks(page);
	const { row } = await _selectAndHover(page, 0);
	await expect(row.locator('.deck-btns')).toBeVisible();
	await page.waitForTimeout(PAST_DBLCLICK_GUARD_MS);

	const reach = await page.evaluate(() => {
		const first = document.querySelector<HTMLElement>('[data-testid="track-row"]');
		if (first === null) throw new Error('no first row');
		const box = first.querySelector<HTMLElement>('.deck-btns');
		if (box === null) throw new Error('the first row rendered no quick-load box');
		const boxRect = box.getBoundingClientRect();
		const unreachable: string[] = [];
		for (const target of box.querySelectorAll<HTMLElement>('button')) {
			const rect = target.getBoundingClientRect();
			const x = rect.left + rect.width / 2;
			const y = rect.top + rect.height / 2;
			const hit = document.elementFromPoint(x, y);
			if (hit !== null && target.contains(hit)) continue;
			const stack = document
				.elementsFromPoint(x, y)
				.slice(0, 5)
				.map((e) => `${e.tagName}.${e.className}[z=${getComputedStyle(e).zIndex}]`)
				.join(' > ');
			unreachable.push(`${target.textContent?.trim()} -> ${stack}`);
		}
		return { unreachable, boxTop: boxRect.top, boxHeight: boxRect.height };
	});

	expect(reach.boxHeight, 'a zero-height box is nothing to reach').toBeGreaterThan(0);
	expect(
		reach.boxTop,
		'the first row picker must still be on screen, not pushed off the top'
	).toBeGreaterThan(0);
	expect(
		reach.unreachable,
		'every button of the FIRST row picker must resolve to itself: above row 0 there is only the sticky header and the scroll container clip'
	).toEqual([]);
});
