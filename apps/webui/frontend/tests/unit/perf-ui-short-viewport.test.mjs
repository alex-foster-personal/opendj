import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

// PERF-UI-01 (issue #2303): default Playwright 1280x720 leftover after
// topbar 28 + 4 * 43 waverows + deck floor 497 is ~23px, so the browser
// panel collapses and playlist-all-tracks / track-row sit outside the
// viewport. Compact the MORE-mode wavestack at max-height 799px (800px
// LIBUX-01 tests stay on the 43px row) and collapse Next/Recommended so
// thead + one track row plus the playlist tree row fit without a page
// scroll. LESS mode already has spare height and must not be auto-applied
// (that hides decks 3/4).
//
// Regression lines:
// - if the compact --rb-waverow-h is missing or stays at 43px then 720px
//   leftover is ~23px again and agentic-testing SOURCE clicks time out
// - if the media query uses 800px then the 1280x800 LIBUX-01 suite silently
//   changes its wavestack
// - if the selector matches LESS mode then decks 3/4 stay hidden AND the
//   library-min-5-rows brace-scoped `.perf-root {` regex may drift
// - if leftover at 720px cannot cover pane-header + chevron + bottom-bar +
//   thead + one compact row then track-row is clipped even after compacting
// - if BrowserPanel does not auto-collapse Next/Recommended at the same
//   breakpoint (reopenable via the existing chevron) the suggestion strip
//   eats the one visible track row
// - if .pane-header is allowed to wrap at short height the 24px floor
//   becomes ~45px and the row is clipped again
// - if the playlist history list stays at its 72px max-height the tree
//   scroll is crushed and playlist-all-tracks leaves the viewport

const PAGE_PATH = fileURLToPath(
	new URL('../../src/routes/performance/+page.svelte', import.meta.url)
);
const TOPBAR_PATH = fileURLToPath(
	new URL('../../src/lib/components/rb/TopBar.svelte', import.meta.url)
);
const BROWSER_PANEL_PATH = fileURLToPath(
	new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)
);
const HISTORY_PANEL_PATH = fileURLToPath(
	new URL('../../src/lib/components/rb/browser/PlaylistHistoryPanel.svelte', import.meta.url)
);
const THEME_PATH = fileURLToPath(new URL('../../src/lib/rb/theme.css', import.meta.url));
const SUPPORT_PATH = fileURLToPath(
	new URL('../../src/lib/components/rb/browser/browser-panel-support.ts', import.meta.url)
);

const TOPBAR_PX = 28;
const DECK_FLOOR_PX = 497;
const SHORT_VIEWPORT_PX = 720;
const PANE_HEADER_PX = 24;
const COLLAPSE_BAR_PX = 12;
const BOTTOM_BAR_PX = 18;
const THEAD_PX = 20;
const COMPACT_ROW_PX = 22;
const MIN_LIBRARY_CHROME_PX =
	PANE_HEADER_PX + COLLAPSE_BAR_PX + BOTTOM_BAR_PX + THEAD_PX + COMPACT_ROW_PX;

function firstMatch(source, pattern, label) {
	const m = source.match(pattern);
	assert.ok(m, `expected to find ${label}`);
	return m;
}

test('theme.css keeps the tall-viewport --rb-waverow-h at 43px', () => {
	const theme = readFileSync(THEME_PATH, 'utf8');
	const m = firstMatch(theme, /--rb-waverow-h:\s*(\d+)px/, "theme.css's --rb-waverow-h");
	assert.equal(Number(m[1]), 43, 'tall viewports must keep the shipped 43px waverow');
});

test('MORE-mode compact wavestack at max-height 799px leaves room for one track row at 720px', () => {
	const pageSource = readFileSync(PAGE_PATH, 'utf8');

	assert.match(
		pageSource,
		/@media\s*\(max-height:\s*799px\)/,
		'a @media (max-height: 799px) rule on +page.svelte'
	);
	assert.match(
		pageSource,
		/@media\s*\(max-height:\s*799px\)[\s\S]*?\.perf-root:not\(\.deck-layout-less\)\s*\{/,
		'compact waverow must target MORE mode only via .perf-root:not(.deck-layout-less)'
	);
	const wave = firstMatch(
		pageSource,
		/@media\s*\(max-height:\s*799px\)[\s\S]*?\.perf-root:not\(\.deck-layout-less\)\s*\{[^}]*--rb-waverow-h:\s*(\d+)px/,
		'a compact --rb-waverow-h inside the short-viewport media query'
	);
	const wavePx = Number(wave[1]);
	assert.ok(
		wavePx >= 18 && wavePx <= 24,
		`compact --rb-waverow-h must be 18-24px (found ${wavePx}) so leftover at 720px covers one track row`
	);

	const leftover = SHORT_VIEWPORT_PX - TOPBAR_PX - 4 * wavePx - DECK_FLOOR_PX;
	assert.ok(
		leftover >= MIN_LIBRARY_CHROME_PX,
		`720px leftover after compact wavestack is ${leftover}px, need >= ${MIN_LIBRARY_CHROME_PX}px ` +
			'(pane-header + chevron + bottom-bar + thead + one compact row)'
	);

	assert.match(
		pageSource,
		/@media\s*\(max-height:\s*799px\)[\s\S]*?\.word-lane[\s\S]*?display:\s*none/,
		'compact rows are below the 34px lyric-lane floor; WordLane must not paint over the next waverow'
	);

	const topbar = readFileSync(TOPBAR_PATH, 'utf8');
	assert.match(
		topbar,
		/\.lyrics-compact-chip[\s\S]*@media\s*\(max-height:\s*799px\)[\s\S]*display:\s*inline-flex/,
		'TopBar must show the compact lyric-status chip at max-height 799px'
	);
	assert.match(
		topbar,
		/class="bsm-toggle topbar-slot-lyr"[\s\S]*aria-pressed=\{uiPrefs\.lyrics_global\}/,
		'LYR toggle must keep aria-pressed tied to the global lyrics preference'
	);

	// 800px LIBUX-01 tests must keep the default 43px / 497px MORE block.
	const moreBlock = firstMatch(
		pageSource,
		/\.perf-root\s*\{([^}]*)\}/,
		'the default (MORE) .perf-root rule block'
	);
	assert.match(
		moreBlock[1],
		/minmax\(\s*497px,/,
		'MORE deck-area floor must stay 497px outside the short-viewport query'
	);
	assert.match(
		moreBlock[1],
		/calc\(4\s*\*\s*\(var\(--rb-waverow-h\)\s*\+\s*var\(--rb-stemwave-stack-extra,\s*0px\)\)\)/,
		'MORE must still reserve all 4 wavestack rows'
	);
});

test('BrowserPanel auto-collapses Next/Recommended at the same 799px breakpoint and keeps the header to one line', () => {
	const panel = readFileSync(BROWSER_PANEL_PATH, 'utf8');
	const support = readFileSync(SUPPORT_PATH, 'utf8');

	assert.match(
		support,
		/setLibraryPanelsCollapsed/,
		'browser-panel-support must re-export setLibraryPanelsCollapsed so BrowserPanel can collapse without a new import edge'
	);
	assert.match(
		panel,
		/matchMedia\(\s*['"]\(max-height:\s*799px\)['"]\s*\)/,
		'BrowserPanel must observe max-height 799px (same breakpoint as the compact wavestack)'
	);
	assert.match(
		panel,
		/setLibraryPanelsCollapsed\(\s*true\s*\)/,
		'first crossing into a short viewport must collapse Next/Recommended (chevron still reopens them)'
	);

	assert.match(
		panel,
		/@media\s*\(max-height:\s*799px\)[\s\S]*?\.pane-header[\s\S]*?max-height:\s*24px/,
		'.pane-header must stay on its 24px floor at short height (no wrap stealing the track row)'
	);
	assert.match(
		panel,
		/@media\s*\(max-height:\s*799px\)[\s\S]*?\.header-right[\s\S]*?flex-wrap:\s*nowrap/,
		'.header-right must not wrap at short height'
	);
});

test('playlist history list hides at max-height 799px so All Tracks stays in the tree viewport', () => {
	const source = readFileSync(HISTORY_PANEL_PATH, 'utf8');
	const media = firstMatch(
		source,
		/@media\s*\(max-height:\s*799px\)\s*\{([\s\S]*?)\n\t\}/,
		'a @media (max-height: 799px) rule on PlaylistHistoryPanel.svelte'
	);
	assert.match(
		media[1],
		/\.history-list[\s\S]*display:\s*none/,
		'.history-list (max-height 72px) must hide at short viewports so tree-scroll is not crushed'
	);
});
