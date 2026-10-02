import { expect, test } from '@playwright/test';

// LIBUX-01 e2e smoke - "Library panel should be slightly greedier - minimum
// 5 rows of songs." A real browser is the only thing that can prove the CSS
// cascade (perf-root's grid reservation + TrackTable's flex-shrink floor)
// actually resolves the way the arithmetic in library-min-5-rows.test.mjs
// predicts; that unit test only checks the source, not the render.
//
// No library data is required: TrackTable always renders its <table><thead>
// regardless of row count (see TrackTable.svelte - the empty-state message
// is an overlay sibling, not a table replacement), so this is a pure
// geometry check.
//
// Two floors compete for the same vertical space (see +page.svelte's
// perf-root comment, PR #1007 discussions r3921198996, r3921321752,
// r3921443899 and r3923591731): the deck area's documented content-tight
// floor (protected via `minmax(<floor>px, ...)`, since `.rb-deck` uses
// overflow: hidden and a shorter box genuinely clips controls - 497px, the
// deck's own two-deck-column requirement), and the library's 272px 5-row
// floor. Their sum plus topbar/wave (200px) is 969px, taller than the repo's
// standard 1280x800 viewport, so only ONE floor can be fully satisfied below
// that height. Decks/mixer win the conflict (protecting already-shipped
// controls), so this file tests each floor at the viewport where it is
// actually supposed to hold, instead of asserting both at once somewhere
// neither can be true - and then tests, AT 1280x800, what the losing side
// does with the shortfall, which is where the real defect was.
//
// FIX ROUND 3 note: pin 246b0f5 round 1 briefly raised the MORE deck-area
// floor to 524px (to cover the mixer's own un-collapsed content), which
// moved this 969px threshold to 996px without amending REQUIREMENTS.md's
// LIBUX-01 acceptance line - a real regression two independent reviewers
// (Sol comment 3963434154, P2 BLOCKING; a blinded reviewer separately) both
// caught. Round 3 fixed it the other way: ChannelStrip.svelte's MORE-mode
// margins were compacted so the mixer's real content fits back inside the
// ORIGINAL 497px floor (channel-strip-less-floor.test.mjs's "MORE floor"
// test proves 492.6px required, comfortably under 497), restoring the
// documented 969px threshold instead of moving the requirement. The two
// tests further down this file at 969px and 720px are the boundary
// regression coverage for that finding.

// 1000px clears the >= 969px threshold at which BOTH floors fit (497
// deck/mixer + 272 library + 200 topbar/wave), so the 5-row guarantee is
// actually claimable here. Below it the guarantee does not hold and the
// tests further down assert what happens instead, rather than pretending it
// does.
const TALL_VIEWPORT = { width: 1280, height: 1000 };
// thead (20px, fixed) + 5 * compact row height (22px, the default density) +
// a 17px classic-scrollbar-gutter allowance - mirrors TrackTable.svelte's
// `.tt-root { min-height: calc(20px + 5 * var(--tt-row-h) + 17px +
// var(--tt-truncation-h)) }` with no truncation banner showing
// (PR #1007 discussion r3921198996: table-wrap's default column widths
// exceed this viewport's width, so a horizontal scrollbar is real; this
// sandbox's Chromium happens to render overlay scrollbars, which cost 0
// layout height, so this assertion cannot itself distinguish "budgeted the
// 17px and it went unused" from "the budget is wrong" - see NOT-verified in
// the PR body). 1000px is tall enough that the deck-area floor (497px, FIX
// ROUND 3 restored - see the header comment above) is not in the way (969px
// needed for both floors at once, 1000 > 969).
const MIN_TABLE_WRAP_HEIGHT = 20 + 5 * 22 + 17;

test('performance: library table-wrap keeps a 5-row floor once the window is tall enough for both floors', async ({
	page
}) => {
	await page.setViewportSize(TALL_VIEWPORT);
	await page.goto('/performance');

	const tableWrap = page.locator('.table-wrap');
	await expect(tableWrap).toBeVisible();

	const box = await tableWrap.boundingBox();
	expect(box).not.toBeNull();
	// -1px tolerance for subpixel layout rounding across browser engines.
	expect(box!.height).toBeGreaterThanOrEqual(MIN_TABLE_WRAP_HEIGHT - 1);
});

// Content-tight per-deck floor from +page.svelte's own perf-root comment
// ("one deck needs 248px"): header 75 + strip 28 + main-row 113 + stems 16 +
// padding/gaps 16. PR #1007 discussion r3921321752 found that LIBUX-01's
// library reservation pushed decks to ~172px at the repo's standard
// 1280x800 viewport, well below this floor, clipping the bottom-most
// control row (StemRow's `.stems`) via `.rb-deck`'s overflow: hidden.
const STANDARD_VIEWPORT = { width: 1280, height: 800 };
const MIN_DECK_HEIGHT = 248;

test('performance: deck keeps its content-tight floor (no control clipping) at the standard 1280x800 viewport', async ({
	page
}) => {
	await page.setViewportSize(STANDARD_VIEWPORT);
	await page.goto('/performance');

	const deck = page.locator('.rb-deck').first();
	await expect(deck).toBeVisible();

	const deckBox = await deck.boundingBox();
	expect(deckBox).not.toBeNull();
	expect(deckBox!.height).toBeGreaterThanOrEqual(MIN_DECK_HEIGHT - 1);

	// The bottom-most control row must actually render inside the deck's
	// visible box, not just exist in the DOM behind overflow: hidden.
	const stems = deck.locator('.stems');
	await expect(stems).toBeVisible();
	const stemsBox = await stems.boundingBox();
	expect(stemsBox).not.toBeNull();
	expect(stemsBox!.y + stemsBox!.height).toBeLessThanOrEqual(deckBox!.y + deckBox!.height + 1);
});

// PR #1007 discussion r3921443899 (P2/BLOCKING): "at the configured 1280x800
// Playwright viewport ... only 103px for the browser, versus the 255px needed
// ... Because `.perf-root` hides overflow, TrackTable's larger `min-height`
// cannot make those rows visible."
//
// The arithmetic is confirmed and unfixable at that height: the deck floor
// (497px, itself a BLOCKING finding from r3921321752 and asserted above) plus
// the library floor (272px) plus topbar/wave (200px) is 969px, so at 800px
// one of the two must give and the decks win. What was a real, separate
// defect is what the losing side DID: `.tt-root`'s min-height is absolute, so
// the table kept its floor height and painted straight out of the panel -
// measured on main at 1280x800, `.tt-root` ran to y=869 in an 800px window,
// `.perf-root` reported scrollHeight 895 against clientHeight 800 (95px of UI
// outside the window), and a hit test at the centre of the browser's bottom
// bar landed inside `.table-wrap`, i.e. the track list was painted on top of
// the bottom bar.
//
// `.list-panel { overflow: hidden }` (BrowserPanel.svelte) confines the
// shortfall to "fewer rows visible". These two tests are the red-then-green
// evidence for that: both fail on main (scrollHeight 895 > 801; hit test in
// `.tt-root`) and pass with the clip in place.
const OVERFLOW_TOLERANCE_PX = 2;

test('performance: the library floor never pushes UI outside the window at the standard 1280x800 viewport', async ({
	page
}) => {
	await page.setViewportSize(STANDARD_VIEWPORT);
	await page.goto('/performance');
	await expect(page.locator('.table-wrap')).toBeVisible();

	const overflow = await page.evaluate(() => {
		const root = document.querySelector('.perf-root');
		if (root === null) {
			throw new Error('.perf-root not found');
		}
		return { scrollHeight: root.scrollHeight, clientHeight: root.clientHeight };
	});

	expect(overflow.scrollHeight).toBeLessThanOrEqual(
		overflow.clientHeight + OVERFLOW_TOLERANCE_PX
	);
});

test('performance: the track list never paints over the browser bottom bar at 1280x800', async ({
	page
}) => {
	await page.setViewportSize(STANDARD_VIEWPORT);
	await page.goto('/performance');
	await expect(page.locator('.table-wrap')).toBeVisible();

	const hit = await page.evaluate(() => {
		const bar = document.querySelector('.bottom-bar');
		if (bar === null) {
			throw new Error('.bottom-bar not found');
		}
		const box = bar.getBoundingClientRect();
		const target = document.elementFromPoint(
			Math.round(box.x + box.width / 2),
			Math.round(box.y + box.height / 2)
		);
		if (target === null) {
			throw new Error('nothing hit-tested at the bottom bar centre');
		}
		return {
			insideBottomBar: target.closest('.bottom-bar') !== null,
			insideTrackTable: target.closest('.tt-root') !== null
		};
	});

	expect(hit.insideBottomBar).toBe(true);
	expect(hit.insideTrackTable).toBe(false);
});

// PR #1007 discussion r3923591731 (P2/BLOCKING): when a whole-collection
// search returns more than 200 hits, `.truncated-note` renders as a
// non-shrinking sibling of `.table-wrap` INSIDE `.tt-root`'s fixed minimum
// height, so the banner is paid for out of the five rows.
//
// This asserts the fix where the defect actually lives - in the CSS cascade,
// on the real `/performance` page, against the real TrackTable element and
// the real `data-truncated` attribute production writes from
// `provider.truncated`. It drives that attribute directly rather than through
// a truncating search, because no self-contained harness in this repo serves
// a library with more than 200 matching rows; the unit test
// (library-min-5-rows.test.mjs) covers the other half by asserting the
// attribute is bound to `provider.truncated` and that the banner markup is
// gated on the same read. See NOT-verified in the PR body.
// Pin 862cd3 LESS mode: decks 3/4 collapse to 0 height in place, and the
// whole point is that the freed vertical space actually reaches the library
// row (library-min-5-rows.test.mjs proves the CSS source says so; this is
// the render-level proof). At the standard 1280x800 viewport MORE mode
// cannot satisfy both floors at once (see the 969px arithmetic above), but
// LESS mode's own floors are smaller - topbar 28 + wavestack (2 rows) 86 +
// deck-area floor 400 (pin 246b0f5, Sol-P1-fixed - see below) = 514px, well
// under 800 - so LESS should still get more real library height than MORE
// at the exact same viewport, while decks 1/2 stay unclipped and decks 3/4
// stay mounted (just visually collapsed, not removed - they keep receiving
// IPC/audio per the pin).
//
// Pin 246b0f5 (follow-on to 862cd3, the maintainer: "you ddin't move the 1/2 levels so
// now can't be seen in LESS"): the deck-area floor above is no longer just
// one deck panel's own 248px requirement - `<Mixer />` shares this same grid
// row (`.deck-area`'s grid-template-areas is 'decks-left mixer decks-right',
// ONE row) and its own LESS-mode content (decks 1/2's fader/level-meter/EQ/
// STEM, none of which pin 862cd3 ever collapses) needed more height than
// that to avoid `.rb-mixer`'s `overflow: hidden` clipping them - exactly
// what the maintainer reported. So the floor grew from 248 to 388, then to 400 (Sol P1
// finding, comment 3963232874: the 388 total omitted the mixer's own 12px
// padding/border chrome) - channel-strip-less-floor.test.mjs derives and
// pins that number - and the library's gain over MORE shrank accordingly -
// the maintainer's own words authorizing that trade: "probably a little bit more
// height taken from library ... but optimize it visually." The assertion
// below requires a concrete, meaningful gain (see MIN_LESS_LIBRARY_GAIN_PX
// below) rather than the old fixed "one whole collapsed deck column" amount,
// which this pin's fix can no longer deliver in full.
const LESS_MODE_CHORD = process.platform === 'darwin' ? 'Meta+2' : 'Control+2';

/** `.table-wrap`'s on-screen height: its box clipped by every ancestor whose
 *  overflow is not visible, and by the viewport. */
async function visibleTableWrapHeight(page: import('@playwright/test').Page): Promise<number> {
	return page.locator('.table-wrap').evaluate((wrap) => {
		const box = wrap.getBoundingClientRect();
		let top = Math.max(box.top, 0);
		let bottom = Math.min(box.bottom, window.innerHeight);
		for (let el = wrap.parentElement; el !== null; el = el.parentElement) {
			const style = getComputedStyle(el);
			if (style.overflowY === 'visible' && style.overflow === 'visible') continue;
			const clip = el.getBoundingClientRect();
			top = Math.max(top, clip.top);
			bottom = Math.min(bottom, clip.bottom);
		}
		return Math.max(0, bottom - top);
	});
}

test('performance: switching to LESS frees real height to the library versus MORE, at the same 1280x800 viewport', async ({
	page
}) => {
	await page.setViewportSize(STANDARD_VIEWPORT);
	await page.goto('/performance');

	const tableWrap = page.locator('.table-wrap');
	await expect(tableWrap).toBeVisible();
	await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible();
	const moreBox = await tableWrap.boundingBox();
	expect(moreBox).not.toBeNull();
	const moreVisible = await visibleTableWrapHeight(page);

	await page.keyboard.press(LESS_MODE_CHORD);
	// The deck-layout transition is CSS-animated (--rb-deck-layout-duration);
	// wait for the class + collapsed deck 3 rather than a fixed timeout.
	await expect(page.locator('.perf-root')).toHaveClass(/deck-layout-less/);
	// Pre-existing bug fixed in passing while verifying this spec for pin
	// 246b0f5 (unrelated to that pin - reproduced identically on unmodified
	// origin/main): the bare `[data-deck='3']` attribute selector ALSO
	// matches WaveRow's own row div (WaveRow.svelte), which keeps a fixed
	// `height: var(--rb-waverow-h)` on itself and only fades via opacity -
	// its boundingBox() never collapses, so `.first()` here always resolved
	// to that element (DOM order: WaveformStack renders before .deck-area)
	// and this `.poll` spun for the full 15s timeout before failing. Scoped
	// to `.rb-deck` - the deck PANEL that genuinely collapses via
	// `.deck-col [data-deck='3']`'s max-height rule - like
	// performance-less-mode-mixer-levels.spec.ts already does.
	const deck3 = page.locator(".rb-deck[data-deck='3']").first();
	await expect
		.poll(async () => (await deck3.boundingBox())?.height ?? -1)
		.toBeLessThanOrEqual(10);

	const lessBox = await tableWrap.boundingBox();
	expect(lessBox).not.toBeNull();
	await expect
		.poll(async () => visibleTableWrapHeight(page), { timeout: 10_000 })
		.toBeGreaterThan(moreVisible);
	const lessVisible = await visibleTableWrapHeight(page);

	// LESS must still give the library a MEANINGFUL amount more room than
	// MORE, not just any-positive-px - pin 246b0f5 shrank the margin (the
	// mixer's real LESS-mode content now sets the deck-area floor, not just
	// one deck panel - see the header comment above), so this no longer
	// asserts a full collapsed-deck-column's worth. A bare `> 0` would let a
	// future change shrink the gain to 1px and still pass, exactly the
	// regression this test exists to catch.
	//
	// FIX ROUND 2 correction: this threshold was previously 150px, carried
	// over from library-min-5-rows.test.mjs's *grid-row* reservation math
	// ((4-2 wavestack rows)*43 + (524-400 deck floor) = 210px achievable
	// ceiling, 150 pinned under it). That arithmetic is correct for the
	// `.rb-browser` grid row itself (confirmed live this round: MORE reserves
	// it 76px tall, LESS 283px, a real 207px row-level gain, matching the
	// unit test) - but this assertion measures `.table-wrap`'s own rendered
	// box, a DIFFERENT, downstream quantity, and the two are not 1:1. Round 1
	// could not run Playwright at all in its worktree (no seeded library, see
	// the PR's "Not verified"), so this 150px number was never actually
	// checked against a real render; it silently assumed every freed row-px
	// lands on `.table-wrap`. It does not: `.tt-root`'s LIBUX-01 min-height is
	// an ABSOLUTE floor (147px) that MORE mode already sits at (clipped,
	// per `.list-panel{overflow:hidden}`'s own comment above) - freed height
	// first goes toward un-clipping that already-floored box, and only past
	// that point does `.table-wrap` itself grow taller. Measured live
	// (Playwright, this fixture, 1280x800): moreBox.height=147 (the floor,
	// clipped), lessBox.height=206, a real 59px gain - genuine and positive,
	// just far short of the row-level 207px. 40px is pinned comfortably under
	// that measured 59px (19px headroom), while still catching a regression
	// toward a token few-px "gain": it fails just as hard on a 1px gain as
	// the old 150px did.
	//
	// PR #4082 correction: the BOX comparison above stopped holding once the
	// Next / Recommended strips (`.library-panels-strips`, 52px + a 12px
	// collapse bar, acc92778) joined `.list-panel`'s column. Measured live at
	// 1280x800 on this fixture: `.table-wrap`'s box is 147px (its floor) in
	// BOTH modes, because in LESS the strips take the freed px that used to
	// grow it, while `.rb-browser` itself still grows 76px -> 282px. So the
	// box never moves, and the real gain is in what the user SEES: in MORE
	// that floored box is clipped by `.list-panel{overflow:hidden}`, in LESS
	// it is not. Measure the VISIBLE height (the box intersected with every
	// clipping ancestor and the viewport), which is the quantity this test's
	// title claims; the box delta is kept as a non-negative sanity check.
	const MIN_LESS_LIBRARY_GAIN_PX = 40;
	expect(lessBox!.height).toBeGreaterThanOrEqual(moreBox!.height);
	expect(
		lessVisible - moreVisible,
		`visible .table-wrap height MORE=${moreVisible}px LESS=${lessVisible}px`
	).toBeGreaterThanOrEqual(MIN_LESS_LIBRARY_GAIN_PX);

	// Decks 1/2 must stay fully unclipped in LESS, same floor as MORE.
	const deck1 = page.locator('.rb-deck').first();
	await expect(deck1).toBeVisible();
	const deck1Box = await deck1.boundingBox();
	expect(deck1Box).not.toBeNull();
	expect(deck1Box!.height).toBeGreaterThanOrEqual(MIN_DECK_HEIGHT - 1);

	// Decks 3/4 stay MOUNTED (present in the DOM), just visually collapsed -
	// pin 862cd3's whole contract is chrome-only hiding, never unmounting.
	await expect(deck3).toBeAttached();
	const deck4 = page.locator("[data-deck='4']").first();
	await expect(deck4).toBeAttached();
});

test('performance: the truncation banner adds its own height to the 5-row floor', async ({
	page
}) => {
	await page.setViewportSize(TALL_VIEWPORT);
	await page.goto('/performance');

	const table = page.locator('.tt-root');
	await expect(table).toBeVisible();

	const measured = await page.evaluate(() => {
		const root = document.querySelector('.tt-root');
		if (root === null) {
			throw new Error('.tt-root not found');
		}
		const read = () => ({
			minHeight: Number.parseFloat(getComputedStyle(root).minHeight),
			bannerHeight: Number.parseFloat(
				getComputedStyle(root).getPropertyValue('--tt-truncation-note-h')
			)
		});
		const before = read();
		const previous = root.getAttribute('data-truncated');
		root.setAttribute('data-truncated', 'true');
		const after = read();
		root.setAttribute('data-truncated', previous ?? 'false');
		return { before, after };
	});

	expect(measured.before.bannerHeight).toBeGreaterThan(0);
	expect(measured.after.minHeight - measured.before.minHeight).toBeCloseTo(
		measured.before.bannerHeight,
		1
	);
});

// FIX ROUND 3 boundary regression coverage for the two Sol BLOCKING findings
// on +page.svelte:281 (comments 3963623911 P1, 3963434154 P2), both about
// this file's own deck-area floor arithmetic:
//
// - P2 (LIBUX-01's own 969px threshold): the five-row guarantee must hold at
//   any window >= 969px tall (REQUIREMENTS.md LIBUX-01). Round 1's 524px
//   floor silently moved that boundary to 996px; round 3 restored 497px so
//   969px is real again. This test sits exactly ON that boundary rather than
//   comfortably above it (TALL_VIEWPORT, 1000px) - the previous tests in
//   this file already prove the interior; this one proves the edge.
// - P1 (the short-window contract, REQUIREMENTS.md ~2581-2586): below the
//   969px threshold the shortfall must cost ROWS ONLY, never let the deck
//   area's own overflow intercept clicks meant for the library. Round 1's
//   524px floor collapsed the browser row to ~0px at the repo's default
//   1280x720 viewport, which is exactly what broke
//   autoplay-explainer-placement.spec.ts and context-menu.spec.ts (both now
//   pass again at the native, unmodified 720px viewport with no per-spec
//   override - see those files). This test is the direct, page-agnostic
//   proof: a real Playwright click (which itself performs the same
//   actionability checks - visible, stable, receives-pointer-events - that
//   made those two specs time out on round 1's floor) must reach and
//   select the first track row, and the same click on the row's context
//   menu trigger must open its menu rather than intercepting on deck/
//   bottom-bar chrome painted over it.
const LIBUX01_THRESHOLD_VIEWPORT = { width: 1280, height: 969 };
const SHORT_WINDOW_VIEWPORT = { width: 1280, height: 720 };

test('performance: LIBUX-01 five-row guarantee holds exactly at its documented 969px threshold', async ({
	page
}) => {
	await page.setViewportSize(LIBUX01_THRESHOLD_VIEWPORT);
	await page.goto('/performance');

	const tableWrap = page.locator('.table-wrap');
	await expect(tableWrap).toBeVisible();

	const box = await tableWrap.boundingBox();
	expect(box).not.toBeNull();
	// -1px tolerance for subpixel layout rounding across browser engines,
	// same tolerance the TALL_VIEWPORT test above uses.
	expect(box!.height).toBeGreaterThanOrEqual(MIN_TABLE_WRAP_HEIGHT - 1);

	// Decks 1/2 must stay at their own content-tight floor too - the 969px
	// threshold is only meaningful if BOTH floors hold simultaneously here.
	const deck1 = page.locator('.rb-deck').first();
	await expect(deck1).toBeVisible();
	const deck1Box = await deck1.boundingBox();
	expect(deck1Box).not.toBeNull();
	expect(deck1Box!.height).toBeGreaterThanOrEqual(MIN_DECK_HEIGHT - 1);

	// `.tt-root`'s min-height floor (147px, what the box.height check above
	// reads) holds REGARDLESS of how much room its container actually has -
	// getBoundingClientRect() reports the box's own un-clipped layout size
	// even when `.list-panel`'s `overflow: hidden` (BrowserPanel.svelte) is
	// clipping it out of view, and `.perf-root`'s own scrollHeight does not
	// grow either, because that clip absorbs the overflow internally without
	// ever reaching the grid track (confirmed live: mutating the deck-area
	// floor to 650px at this exact viewport still read table-wrap box.height
	// 147 and perf-root scrollHeight == clientHeight, while the rows were
	// actually clipped invisible - neither of the checks above would have
	// caught that regression). So the real proof that the five rows are
	// genuinely VISIBLE, not just present in the DOM, is that table-wrap's
	// own box fits entirely inside `.list-panel`'s box - if the deck-area
	// floor eats into the library's 272px budget at 969px, table-wrap's
	// bottom edge runs past `.list-panel`'s and is silently clipped away.
	const listPanel = page.locator('.list-panel');
	await expect(listPanel).toBeVisible();
	const listPanelBox = await listPanel.boundingBox();
	expect(listPanelBox).not.toBeNull();
	expect(box!.y + box!.height).toBeLessThanOrEqual(listPanelBox!.y + listPanelBox!.height + 1);
});

test('performance: at the short 1280x720 window, the shortfall costs library rows only - it never lets deck/bottom-bar chrome intercept a click on the track list', async ({
	page
}) => {
	await page.setViewportSize(SHORT_WINDOW_VIEWPORT);
	await page.goto('/performance');
	await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible({ timeout: 30_000 });

	// The page itself must not overflow its own viewport (same invariant the
	// 1280x800 test above proves; this is the same check at the window this
	// pin's floor arithmetic actually has to defend, 720px, not 800px).
	const overflow = await page.evaluate(() => {
		const root = document.querySelector('.perf-root');
		if (root === null) {
			throw new Error('.perf-root not found');
		}
		return { scrollHeight: root.scrollHeight, clientHeight: root.clientHeight };
	});
	expect(overflow.scrollHeight).toBeLessThanOrEqual(overflow.clientHeight + OVERFLOW_TOLERANCE_PX);

	// The direct proof Sol's P1 finding names: a real Playwright click - which
	// itself performs the browser's actionability checks (scrolls the target
	// into view, waits for it to be stable and to actually receive pointer
	// events at its resolved coordinates) - must reach the first track row
	// rather than timing out with "subtree intercepts pointer events" the way
	// context-menu.spec.ts and autoplay-explainer-placement.spec.ts did
	// against round 1's 524px floor. A raw, unscrolled `elementFromPoint` at
	// the row's pre-click boundingBox is NOT equivalent to this - Chromium's
	// own `.click()` scrolls the element into view first (confirmed live:
	// this row's boundingBox.y moves from 742 to 689 across the click), so
	// asserting against the pre-scroll box would fail even on a fully
	// healthy layout. The click itself, not a static coordinate, is the real
	// contract.
	const track = page.locator('[data-testid="track-row"]').first();
	await track.click({ timeout: 5_000 });
	await expect(track).toHaveClass(/rb-row-selected/);

	// And the same for the row's own context menu (context-menu.spec.ts's
	// exact assertion, re-run at this exact 720px boundary rather than
	// relying on that spec happening to run at the same viewport).
	await track.click({ button: 'right', timeout: 5_000 });
	await expect(page.locator('[data-testid="context-menu"]')).toContainText('Load to deck 1');
	await page.keyboard.press('Escape');
});

const MORE_MODE_CHORD = process.platform === 'darwin' ? 'Meta+1' : 'Control+1';

async function tableWrapHeight(page: import('@playwright/test').Page): Promise<number> {
	return page.locator('.table-wrap').evaluate((el) => el.getBoundingClientRect().height);
}

async function scrollRowNearViewportBottom(
	page: import('@playwright/test').Page,
	rowIndex: number
): Promise<void> {
	await page.evaluate((index) => {
		const wrap = document.querySelector('.table-wrap');
		const row = document.querySelectorAll('[data-testid="track-row"]')[index];
		if (!(wrap instanceof HTMLElement) || !(row instanceof HTMLElement)) {
			throw new Error('table-wrap or target row missing');
		}
		const w = wrap.getBoundingClientRect();
		const r = row.getBoundingClientRect();
		const delta = r.bottom - w.bottom + 2;
		wrap.scrollTop = Math.max(0, wrap.scrollTop + delta);
	}, rowIndex);
}

async function selectedRowFullyVisible(page: import('@playwright/test').Page): Promise<boolean> {
	return page.evaluate(() => {
		const wrap = document.querySelector('.table-wrap');
		const row = document.querySelector('[data-testid="track-row"].rb-row-selected');
		if (!(wrap instanceof HTMLElement) || !(row instanceof HTMLElement)) return false;
		const w = wrap.getBoundingClientRect();
		const r = row.getBoundingClientRect();
		return r.top >= w.top - 0.5 && r.bottom <= w.bottom + 0.5;
	});
}

test('performance: selected library row stays visible when toggling MORE and LESS (LIBUX-18, issue #3984)', async ({
	page
}) => {
	await page.setViewportSize({ width: 1280, height: 1000 });
	await page.goto('/performance');
	const tableWrap = page.locator('.table-wrap');
	await expect(tableWrap).toBeVisible();
	const rows = page.locator('[data-testid="track-row"]');
	const count = await rows.count();
	expect(count).toBeGreaterThanOrEqual(8);

	// LESS first: select a lower row near the bottom of the expanded viewport, then shrink to MORE.
	await page.locator('.deck-layout-btn').filter({ hasText: 'LESS' }).click();
	await expect(page.locator('.perf-root')).toHaveClass(/deck-layout-less/);
	const lessHeight = await tableWrapHeight(page);
	expect(lessHeight).toBeGreaterThan(100);

	const shrinkTarget = rows.nth(7);
	await tableWrap.evaluate((el) => {
		el.scrollTop = 0;
	});
	await scrollRowNearViewportBottom(page, 7);
	await shrinkTarget.click();
	await expect(shrinkTarget).toHaveClass(/rb-row-selected/);
	expect(await selectedRowFullyVisible(page)).toBe(true);
	const shrinkStableId = await shrinkTarget.getAttribute('data-stable-id');
	expect(shrinkStableId).toBeTruthy();

	await page.locator('.deck-layout-btn').filter({ hasText: 'MORE' }).click();
	await expect(page.locator('.perf-root')).not.toHaveClass(/deck-layout-less/);
	await expect
		.poll(async () => tableWrapHeight(page), { timeout: 10_000 })
		.toBeLessThan(lessHeight - 4);
	await expect
		.poll(async () => selectedRowFullyVisible(page), { timeout: 10_000 })
		.toBe(true);
	await expect(page.locator(`[data-testid="track-row"][data-stable-id="${shrinkStableId}"]`)).toHaveClass(
		/rb-row-selected/
	);

	// Inverse: establish a fresh selection in the smaller MORE viewport, then expand with the keyboard chord.
	await tableWrap.evaluate((el) => {
		el.scrollTop = 0;
	});
	const expandTarget = rows.nth(5);
	await scrollRowNearViewportBottom(page, 5);
	await expandTarget.click();
	await expect(expandTarget).toHaveClass(/rb-row-selected/);
	expect(await selectedRowFullyVisible(page)).toBe(true);
	const expandStableId = await expandTarget.getAttribute('data-stable-id');
	expect(expandStableId).toBeTruthy();
	const moreHeight = await tableWrapHeight(page);

	await page.keyboard.press(LESS_MODE_CHORD);
	await expect(page.locator('.perf-root')).toHaveClass(/deck-layout-less/);
	await expect
		.poll(async () => tableWrapHeight(page), { timeout: 10_000 })
		.toBeGreaterThan(moreHeight + 4);
	await expect
		.poll(async () => selectedRowFullyVisible(page), { timeout: 10_000 })
		.toBe(true);
	await expect(page.locator(`[data-testid="track-row"][data-stable-id="${expandStableId}"]`)).toHaveClass(
		/rb-row-selected/
	);
});
