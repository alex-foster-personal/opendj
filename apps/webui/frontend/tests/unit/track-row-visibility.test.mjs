/**
 * ui-mirror's `browser.visible_rows_count` (#3096): the field read 0 on every
 * build because its selector (`.track-row, [role="row"]`) matches nothing -
 * TrackTable.svelte marks each row `data-testid="track-row"` on a plain
 * `<tr>`, and nothing has the class `track-row`. Fixing only the selector is
 * still wrong: TrackTable virtualizes with an overscan pad
 * (`virtual-window.ts`), so a raw DOM count of the fixed selector overstates
 * the true on-screen row count by the overscan on each side. These tests
 * pin both halves: the selector must match, AND the count must exclude rows
 * that are mounted but clipped out of the scroll container's or the
 * window's visible bounds.
 *
 * The fake `document.querySelectorAll` below recognizes ONLY the correct
 * selector string, exactly like the real DOM would recognize only a
 * selector that actually matches the markup - so reverting
 * `TRACK_ROW_SELECTOR` in track-row-visibility.ts back to the old
 * `.track-row, [role="row"]` makes every positive-count test below fail
 * (verified by hand: reverted, re-ran, all four positive-count assertions
 * failed with 0; restored, re-ran green - see PR body for both runs).
 */
import assert from 'node:assert/strict';
import { after, before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
let realDocument;
let realWindow;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/track-row-visibility.ts');
	realDocument = globalThis.document;
	realWindow = globalThis.window;
});

after(() => {
	if (realDocument === undefined) delete globalThis.document;
	else globalThis.document = realDocument;
	if (realWindow === undefined) delete globalThis.window;
	else globalThis.window = realWindow;
});

const TRACK_ROW_SELECTOR = '[data-testid="track-row"]';
const CONTAINER_RECT = { top: 0, left: 0, right: 800, bottom: 400, width: 800, height: 400 };
const WINDOW = { innerWidth: 1728, innerHeight: 1400 };

function rect({ top, bottom, left = 0, right = 800 }) {
	return { top, left, right, bottom, width: right - left, height: bottom - top };
}

function fakeRow(rowRect, container) {
	return {
		getBoundingClientRect: () => rowRect,
		closest: (selector) => (selector === '.table-wrap' ? container : null)
	};
}

function install(rows) {
	globalThis.document = {
		querySelectorAll: (selector) => (selector === TRACK_ROW_SELECTOR ? rows : [])
	};
	globalThis.window = WINDOW;
}

describe('countVisibleTrackRows', () => {
	it('counts every row that intersects both the scroll container and the window [AC1]', () => {
		const container = { getBoundingClientRect: () => CONTAINER_RECT };
		const rows = [
			rect({ top: 0, bottom: 40 }),
			rect({ top: 40, bottom: 80 }),
			rect({ top: 80, bottom: 120 })
		].map((r) => fakeRow(r, container));
		install(rows);
		assert.equal(mod.countVisibleTrackRows(), 3);
	});

	it('excludes rows the container clips even though they sit inside the window [overscan]', () => {
		// Mirrors #3096's own evidence: 38 rows mounted (overscan included),
		// 30 actually inside the viewport. Two rows here sit above/below the
		// container's own clip band while still being on-page.
		const container = { getBoundingClientRect: () => CONTAINER_RECT };
		const rows = [
			rect({ top: -80, bottom: -40 }), // overscan above the container
			rect({ top: 0, bottom: 40 }),
			rect({ top: 360, bottom: 400 }),
			rect({ top: 440, bottom: 480 }) // overscan below the container
		].map((r) => fakeRow(r, container));
		install(rows);
		assert.equal(mod.countVisibleTrackRows(), 2);
	});

	it('reads 0 when no track row is mounted', () => {
		install([]);
		assert.equal(mod.countVisibleTrackRows(), 0);
	});

	it('reads 0 when the browser panel is collapsed to a zero-size rect [AC2, collapsed]', () => {
		const zero = { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
		const container = { getBoundingClientRect: () => zero };
		const rows = [zero, zero].map((r) => fakeRow(r, container));
		install(rows);
		assert.equal(mod.countVisibleTrackRows(), 0);
	});

	it('reads 0 when the panel is scrolled off the top of the window [AC2, scrolled out of view]', () => {
		// The container itself sits entirely above the window (e.g. the page
		// scrolled the whole browser panel out of view); rows are positioned
		// consistently WITHIN the container's own frame, so a check against
		// the container alone would wrongly pass this as "visible".
		const offscreenContainer = rect({ top: -2000, bottom: -1600 });
		const container = { getBoundingClientRect: () => offscreenContainer };
		const rows = [
			rect({ top: -2000, bottom: -1960 }),
			rect({ top: -1960, bottom: -1920 })
		].map((r) => fakeRow(r, container));
		install(rows);
		assert.equal(mod.countVisibleTrackRows(), 0);
	});

	it('throws if a mounted row has no .table-wrap ancestor', () => {
		const rows = [fakeRow(rect({ top: 0, bottom: 40 }), null)];
		install(rows);
		assert.throws(() => mod.countVisibleTrackRows(), /table-wrap/);
	});
});
