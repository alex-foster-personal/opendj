/**
 * ui-mirror's `browser.visible_rows_count` (#3096): the field read 0 on every
 * build because its selector (`.track-row, [role="row"]`) matches nothing -
 * TrackTable.svelte marks each row `data-testid="track-row"` on a plain
 * `<tr>`, and nothing has the class `track-row`. Fixing only the selector is
 * still wrong in two more ways: TrackTable virtualizes with an overscan pad
 * (`virtual-window.ts`), so a raw DOM count of the fixed selector overstates
 * the true on-screen row count by the overscan on each side; and geometry
 * alone cannot see LIBUX-05's technically-working mode, which hides the
 * whole BrowserPanel via opacity on ITS OWN root element rather than
 * unmounting anything, so a hidden panel's rows keep normal on-page rects.
 * These tests pin all three: the selector must match, the count must
 * exclude rows clipped by the scroll container or the window, and it must
 * exclude rows whose paint chain is opacity/visibility/display hidden.
 *
 * The fake `document.querySelectorAll` below recognizes ONLY the correct
 * selector string, exactly like the real DOM would recognize only a
 * selector that actually matches the markup - so reverting
 * `TRACK_ROW_SELECTOR` in track-row-visibility.ts back to the old
 * `.track-row, [role="row"]` makes every positive-count test below fail
 * (verified by hand: reverted, re-ran, all positive-count assertions failed
 * with 0; restored, re-ran green - see PR body for both runs).
 */
import assert from 'node:assert/strict';
import { after, before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
let realDocument;
let realWindow;
let realGetComputedStyle;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/track-row-visibility.ts');
	realDocument = globalThis.document;
	realWindow = globalThis.window;
	realGetComputedStyle = globalThis.getComputedStyle;
});

after(() => {
	if (realDocument === undefined) delete globalThis.document;
	else globalThis.document = realDocument;
	if (realWindow === undefined) delete globalThis.window;
	else globalThis.window = realWindow;
	if (realGetComputedStyle === undefined) delete globalThis.getComputedStyle;
	else globalThis.getComputedStyle = realGetComputedStyle;
});

const TRACK_ROW_SELECTOR = '[data-testid="track-row"]';
const CONTAINER_RECT = { top: 0, left: 0, right: 800, bottom: 400, width: 800, height: 400 };
const WINDOW = { innerWidth: 1728, innerHeight: 1400 };
const PAINTED_STYLE = { opacity: '1', visibility: 'visible', display: 'block' };

function rect({ top, bottom, left = 0, right = 800 }) {
	return { top, left, right, bottom, width: right - left, height: bottom - top };
}

/** A fake DOM node carrying only what `countVisibleTrackRows` reads:
 * geometry, a `.table-wrap` lookup (rows only), and a `parentElement` chain
 * `getComputedStyle` walks to find an opacity/visibility/display hide
 * applied above the node itself (LIBUX-05 sets it on BrowserPanel's root,
 * never on `.table-wrap` or the row). */
function fakeElement({ rowRect, container = null, parentElement = null, style = {} } = {}) {
	return {
		getBoundingClientRect: () => rowRect,
		closest: (selector) => (selector === '.table-wrap' ? container : null),
		parentElement,
		style: { ...PAINTED_STYLE, ...style }
	};
}

function install(rows) {
	globalThis.document = {
		querySelectorAll: (selector) => (selector === TRACK_ROW_SELECTOR ? rows : [])
	};
	globalThis.window = WINDOW;
	globalThis.getComputedStyle = (node) => node.style;
}

describe('countVisibleTrackRows', () => {
	it('counts every row that intersects both the scroll container and the window [AC1]', () => {
		const container = fakeElement({ rowRect: CONTAINER_RECT });
		const rows = [
			rect({ top: 0, bottom: 40 }),
			rect({ top: 40, bottom: 80 }),
			rect({ top: 80, bottom: 120 })
		].map((r) => fakeElement({ rowRect: r, container }));
		install(rows);
		assert.equal(mod.countVisibleTrackRows(), 3);
	});

	it('excludes rows the container clips even though they sit inside the window [overscan]', () => {
		// Mirrors #3096's own evidence: 38 rows mounted (overscan included),
		// 30 actually inside the viewport. Two rows here sit above/below the
		// container's own clip band while still being on-page.
		const container = fakeElement({ rowRect: CONTAINER_RECT });
		const rows = [
			rect({ top: -80, bottom: -40 }), // overscan above the container
			rect({ top: 0, bottom: 40 }),
			rect({ top: 360, bottom: 400 }),
			rect({ top: 440, bottom: 480 }) // overscan below the container
		].map((r) => fakeElement({ rowRect: r, container }));
		install(rows);
		assert.equal(mod.countVisibleTrackRows(), 2);
	});

	it('reads 0 when no track row is mounted [AC2, LIBUX-02 library-page collapse]', () => {
		// The library page's own collapse bar ({#if !isLibraryPanelsCollapsed()},
		// BrowserPanel.svelte) unmounts TrackTable entirely - this is that case.
		install([]);
		assert.equal(mod.countVisibleTrackRows(), 0);
	});

	it('reads 0 when the panel is scrolled off the top of the window [AC2, scrolled out of view]', () => {
		// The container itself sits entirely above the window (e.g. the page
		// scrolled the whole browser panel out of view); rows are positioned
		// consistently WITHIN the container's own frame, so a check against
		// the container alone would wrongly pass this as "visible".
		const offscreenContainer = fakeElement({ rowRect: rect({ top: -2000, bottom: -1600 }) });
		const rows = [
			rect({ top: -2000, bottom: -1960 }),
			rect({ top: -1960, bottom: -1920 })
		].map((r) => fakeElement({ rowRect: r, container: offscreenContainer }));
		install(rows);
		assert.equal(mod.countVisibleTrackRows(), 0);
	});

	it('reads 0 when the panel is opacity-hidden by technically-working mode [AC2, LIBUX-05]', () => {
		// LIBUX-05 sets opacity + pointer-events on BrowserPanel's OWN root
		// element, never a wrapper div. Rows keep normal on-page rects -
		// opacity is a paint effect, not a layout one - so this defeats a
		// geometry-only check and must be caught by the paint-chain walk.
		const hiddenPanelRoot = fakeElement({
			rowRect: rect({ top: 0, bottom: 500 }),
			style: { opacity: '0' }
		});
		const container = fakeElement({ rowRect: CONTAINER_RECT, parentElement: hiddenPanelRoot });
		const rows = [rect({ top: 0, bottom: 40 }), rect({ top: 40, bottom: 80 })].map((r) =>
			fakeElement({ rowRect: r, container })
		);
		install(rows);
		assert.equal(mod.countVisibleTrackRows(), 0);
	});

	it('counts normally when an ancestor is fully opaque and visible [paint-chain control]', () => {
		// Positive control for the test above: a container with a normal,
		// painted ancestor chain must NOT be treated as hidden.
		const visibleAncestor = fakeElement({ rowRect: rect({ top: 0, bottom: 500 }) });
		const container = fakeElement({ rowRect: CONTAINER_RECT, parentElement: visibleAncestor });
		const rows = [rect({ top: 0, bottom: 40 })].map((r) => fakeElement({ rowRect: r, container }));
		install(rows);
		assert.equal(mod.countVisibleTrackRows(), 1);
	});

	it('evaluates rows against their own scroll container [multi-pane]', () => {
		const firstContainer = fakeElement({ rowRect: CONTAINER_RECT });
		const secondContainer = fakeElement({ rowRect: rect({ top: 600, bottom: 1000 }) });
		const rows = [
			fakeElement({ rowRect: rect({ top: 0, bottom: 40 }), container: firstContainer }),
			fakeElement({ rowRect: rect({ top: 600, bottom: 640 }), container: secondContainer })
		];
		install(rows);
		assert.equal(mod.countVisibleTrackRows(), 2);
	});

	it('throws if a mounted row has no .table-wrap ancestor', () => {
		const rows = [fakeElement({ rowRect: rect({ top: 0, bottom: 40 }), container: null })];
		install(rows);
		assert.throws(() => mod.countVisibleTrackRows(), /table-wrap/);
	});
});
