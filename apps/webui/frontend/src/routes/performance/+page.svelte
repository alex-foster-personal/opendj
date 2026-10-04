<script lang="ts">
	// /performance - rekordbox 7 Performance-mode parity route.
	// Full-viewport grid: topbar / wavestack / decks+mixer / browser
	// (SCREENSHOT-SPEC sections 1-5). The root layout renders this route
	// WITHOUT the app shell (sidebar/topbar/padding) - see +layout.svelte.
	// Palette is scoped under .perf-root (theme.css); never touch :root.
	import { replaceState } from '$app/navigation';
	import { onMount } from 'svelte';
	import '$lib/rb/theme.css';
	import AutoPlayStallBanner from '$lib/components/rb/AutoPlayStallBanner.svelte';
	import BrowserPanel from '$lib/components/rb/BrowserPanel.svelte';
	import CommandBar from '$lib/components/rb/CommandBar.svelte';
	import Deck from '$lib/components/rb/Deck.svelte';
	import EqOverlay from '$lib/components/rb/EqOverlay.svelte';
	import IngestDropModal from '$lib/components/rb/IngestDropModal.svelte';
	import Mixer from '$lib/components/rb/Mixer.svelte';
	import QuickDrawMenuLoader from '$lib/components/rb/QuickDrawMenuLoader.svelte';
	import RescueRestoreProgress from '$lib/components/rb/RescueRestoreProgress.svelte';
	import RustEngineBadge from '$lib/components/rb/RustEngineBadge.svelte';
	import TopBar from '$lib/components/rb/TopBar.svelte';
	// The STAGE overlay is mounted by the root layout from a lazy chunk (see
	// stage-overlay-loader.ts). The top bar's Stage button lives here, so this
	// route takes the chunk with its own first paint: the first press never
	// waits on a fetch, and the bundle budget charges the chunk to the surface
	// that downloads it (performance) rather than to other-lazy.
	import '$lib/components/lyrics/StageOverlay.svelte';
	import WaveformStack from '$lib/components/rb/WaveformStack.svelte';
	import { engine, getDeckState, mixerState } from '$lib/rb/audio-engine.svelte';
	import { readMasterWriteRevision } from '$lib/player/state.svelte';
	import { installPerformanceBrowserIpc } from '$lib/rb/performance-ipc.svelte';
	import { installPerformanceSessionRestore } from '$lib/rb/performance-session.svelte';
	import { installRescueRingWriter } from '$lib/rb/rescue-ring-writer.svelte';
	import { runPerformanceRescueAutoRestore } from '$lib/rb/rescue-restore.svelte';
	import { installDeckLayoutHotkeys } from '$lib/rb/deck-layout-hotkeys';
	import { installMixerPerformanceListeners } from '$lib/rb/mixer-performance-listeners';
	import { hydratePerformanceFeedback } from '$lib/rb/vibe.svelte';
	import { installPerformanceHotkeys } from '$lib/rb/performance-hotkeys';
	import { installPreviewSpaceStop } from '$lib/player/preview-space-stop';
	import { previewCue, stopPreviewCue } from '$lib/player/preview-cue.svelte';
	import { installPlaylistHistoryHotkeys } from '$lib/rb/playlist-history-hotkeys';
	import { installTechnicallyWorkingHotkeys } from '$lib/rb/technically-working-hotkeys';
	import {
		isDeckSlotVisible,
		isEdgeVisible,
		isEqRaised,
		isHomelessVisible,
		isOptRevealActive,
		isTechModeActive
	} from '$lib/rb/technically-working.svelte';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { installAutoPlay } from '$lib/rb/auto-play.svelte';
	import { armAutoPlayNext, cancelAutoPlayNext } from '$lib/rb/auto-play-next.svelte';
	import { registerAutoPlayNextController } from '$lib/rb/performance-ipc.svelte';
	import { installDeckObserverEmitter } from '$lib/sets/deck-observer-install';
	import { installPlayCounter } from '$lib/plays/play-counter';
	import { installUiMirror } from '$lib/rb/ui-mirror';
	import { applyDesktopPassthrough } from '$lib/rb/desktop-passthrough';
	import { createGigTeardownActions, noteGigRuntimeMounted } from '$lib/rb/library-mode-runtime';
	import {
		createFailClosedPerformanceTeardown,
		type PerformanceTeardownFailurePhase
	} from '$lib/rb/performance-route-teardown';

	/**
	 * The deck id, declared inline rather than imported.
	 *
	 * Deliberate, and the same call perf-event-log.ts and
	 * presentation-clock-report.ts make for the same reason: `deck-slots.ts`
	 * is the tree's fan-in ceiling and the quality ratchet holds that key at
	 * its measured floor with ZERO headroom on purpose, so the next importer
	 * reds the gate for every lane at once. A four-member union is not worth
	 * doing that to whoever rebases next.
	 */
	type DeckId = 1 | 2 | 3 | 4;

	/** Which column edge owns a deck's own panel (1&3 left, 2&4 right - see
	 * .deck-area's grid-template-areas below). Waveform rows have no owning
	 * edge of their own; every deck's row lives under 'top'. */
	const DECK_COLUMN_EDGE: Record<DeckId, 'left' | 'right'> = {
		1: 'left',
		3: 'left',
		2: 'right',
		4: 'right'
	};

	let { disposeOnUnmount = true }: { disposeOnUnmount?: boolean } = $props();
	// CMDK-01..03: the Cmd-K bar reads the browser's open playlist and loads
	// through its deck-load path.
	let browserPanel = $state<BrowserPanel | undefined>(undefined);

	const techActive = $derived(isTechModeActive());
	const animate = $derived(uiPrefs.technically_working_animate);
	/** Pin 862cd3: MORE/LESS two-deck performance layout. */
	const deckLayoutLess = $derived(uiPrefs.deck_layout === 'less');
	const showStemWaveforms = $derived(uiPrefs.show_stems);
	const deckLayoutDurationMs = $derived(
		uiPrefs.deck_layout_animate ? uiPrefs.deck_layout_duration_ms : 0
	);
	const eqRaised = $derived(isEqRaised());
	const deckIsLoaded = (deck: DeckId): boolean => getDeckState(deck).stable_id !== null;
	const deckColumnVisible = (deck: DeckId): boolean =>
		isDeckSlotVisible(deck, DECK_COLUMN_EDGE[deck], deckIsLoaded);
	const deckWaveformVisible = (deck: DeckId): boolean => isDeckSlotVisible(deck, 'top', deckIsLoaded);

	// Tech mode makes html/body transparent too (see desktop-passthrough.ts).
	$effect(() => applyDesktopPassthrough(techActive));

	function _reportTeardownError(phase: PerformanceTeardownFailurePhase, error: unknown): void {
		console.error(`performance unmount ${phase} failed`, error);
	}

	async function _gracefulStopForUnmount(): Promise<void> {
		for (const deckId of [1, 2, 3, 4] as const) {
			if (getDeckState(deckId).playing) {
				await engine.pause(deckId);
			}
		}
	}

	onMount(() => {
		noteGigRuntimeMounted();
		const beginTeardown = createFailClosedPerformanceTeardown({
			...createGigTeardownActions({
				readMaster: () => mixerState.master,
				readMasterWriteRevision,
				setMaster: (value) => engine.setMaster(value),
				disposeEngine: () => engine.dispose()
			}),
			gracefulStop: _gracefulStopForUnmount,
			reportError: _reportTeardownError
		});
		// Pin (review thread, #1119): the IPC's `version` flips to 1 the
		// instant it is installed, and a browser-driven test/agent treats that
		// as "safe to query" - see performance-feedback-browser.spec.ts's
		// waitForFunction. Installing it before feedback hydration settles let
		// such a caller observe {count: 0, last_mark: null} despite persisted
		// marks. So the IPC is not published until hydration resolves (success
		// OR failure - a failed hydrate is genuinely "nothing recorded", not a
		// reason to leave `window.musicDjToolsPerformance` unset forever).
	let uninstallIpc: (() => void) | null = null;
		let uninstallSessionRestore: (() => void) | null = null;
		let uninstallRescueRingWriter: (() => void) | null = null;
		let unmounted = false;
		void hydratePerformanceFeedback().finally(async () => {
			if (unmounted) return;
			uninstallIpc = installPerformanceBrowserIpc();
			const rescueHandled = await runPerformanceRescueAutoRestore();
			uninstallSessionRestore = installPerformanceSessionRestore({
				replaceState: (url) => replaceState(url, {}),
				skipDeckRestore: rescueHandled
			});
			const rescueWriter = installRescueRingWriter();
			uninstallRescueRingWriter = () => rescueWriter.dispose();
		});
		// Installed BEFORE the performance hotkeys, and the order is the whole
		// guarantee: both are window capture-phase keydown listeners (the deck
		// Space toggle moved to capture in #4009 for LIBUX-21), and listeners on
		// one target in one phase fire in registration order. Installed second,
		// the deck toggle ran first and Space stopped the room along with the
		// preview (CUEOUT-15 R5).
		const uninstallPreviewSpaceStop = installPreviewSpaceStop({
			isPreviewPlaying: () => previewCue.playing,
			stopPreview: stopPreviewCue
		});
		const uninstallHotkeys = installPerformanceHotkeys();
		const uninstallHistoryHotkeys = installPlaylistHistoryHotkeys();
		const uninstallDeckLayoutHotkeys = installDeckLayoutHotkeys();
		const uninstallTechHotkeys = installTechnicallyWorkingHotkeys();
		const uninstallAutoPlay = installAutoPlay();
		const unregisterAutoPlayNext = registerAutoPlayNextController({
			arm: armAutoPlayNext,
			cancel: cancelAutoPlayNext
		});
		// The set recorder's own-deck source is push-fed: nothing server-side
		// can see these decks, so this is what makes REC record an Open DJ set.
		// It lives here rather than in the root layout because the deck engine
		// only exists on this route.
		const uninstallDeckObserver = installDeckObserverEmitter();
		// PLAYS-01: always-on, unlike the recorder above, so everyday plays
		// count toward the library's play count too.
		const uninstallPlayCounter = installPlayCounter();
		const uninstallUiMirror = installUiMirror();
		const uninstallMixerListeners = installMixerPerformanceListeners();
		return () => {
			unmounted = true;
			try {
				uninstallMixerListeners();
				uninstallUiMirror();
				uninstallDeckObserver();
				uninstallPlayCounter();
				cancelAutoPlayNext();
				unregisterAutoPlayNext();
				uninstallAutoPlay();
				uninstallTechHotkeys();
				uninstallDeckLayoutHotkeys();
				uninstallHistoryHotkeys();
				uninstallHotkeys();
				uninstallPreviewSpaceStop();
				uninstallSessionRestore?.();
				uninstallRescueRingWriter?.();
				uninstallIpc?.();
			} finally {
				if (disposeOnUnmount) void beginTeardown();
			}
		};
	});
</script>

<svelte:head>
	<title>Open DJ - Performance</title>
</svelte:head>

<div
	class="perf-root"
	style={`--rb-deck-layout-duration: ${deckLayoutDurationMs}ms`}
	class:tw-active={techActive}
	class:tw-no-animate={!animate}
	class:deck-layout-less={deckLayoutLess}
	class:show-stem-waveforms={showStemWaveforms}
	class:tw-top-visible={isEdgeVisible('top', deckIsLoaded)}
	class:tw-left-visible={isEdgeVisible('left', deckIsLoaded)}
	class:tw-right-visible={isEdgeVisible('right', deckIsLoaded)}
	class:tw-bottom-visible={isEdgeVisible('bottom', deckIsLoaded)}
	class:tw-homeless-visible={isHomelessVisible()}
	class:tw-opt-reveal={isOptRevealActive()}
	class:tw-deck1-visible={deckColumnVisible(1)}
	class:tw-deck2-visible={deckColumnVisible(2)}
	class:tw-deck3-visible={deckColumnVisible(3)}
	class:tw-deck4-visible={deckColumnVisible(4)}
	class:tw-wave1-visible={deckWaveformVisible(1)}
	class:tw-wave2-visible={deckWaveformVisible(2)}
	class:tw-wave3-visible={deckWaveformVisible(3)}
	class:tw-wave4-visible={deckWaveformVisible(4)}
>
	<IngestDropModal />
	<TopBar />
	<RescueRestoreProgress />
	<!-- PLAY-08: fixed-position, outside the grid, so a mid-set AutoPlay stop
	     cannot reflow the decks while it explains itself. -->
	<AutoPlayStallBanner />
	<RustEngineBadge />
	<WaveformStack />
	<div class="deck-area">
		<div class="deck-col">
			<Deck deckId={1} />
			<Deck deckId={3} />
		</div>
		<Mixer />
		<div class="deck-col">
			<Deck deckId={2} />
			<Deck deckId={4} />
		</div>
	</div>
	<BrowserPanel bind:this={browserPanel} />
	{#if browserPanel}
		<CommandBar
			rows={browserPanel.commandBarRows()}
			playlistTitle={browserPanel.commandBarTitle()}
			load={browserPanel.commandBarLoad}
			soloVocals={browserPanel.commandBarSoloVocals}
		/>
	{/if}
	<QuickDrawMenuLoader />
	{#if eqRaised}
		<EqOverlay />
	{/if}
</div>

<style>
	/* Fixed to the viewport so the clone owns the whole window regardless of
	 * any surrounding document flow. */
	/* Toggled on document.documentElement/body by the $effect above - app.css
	 * owns their normal opaque background, this only overrides it while a
	 * /performance instance is in tech mode. */
	:global(html.tw-desktop-passthrough),
	:global(body.tw-desktop-passthrough) {
		background: transparent;
	}

	.perf-root {
		position: fixed;
		inset: 0;
		display: grid;
		grid-template-areas:
			'topbar'
			'wavestack'
			'deckarea'
			'browser';
		/* Deck ceiling is content-tight, not generous: one deck needs 248px
		 * (header 75 + strip 28 + main-row 113 + stems 16 + padding/gaps 16),
		 * so a two-deck column needs 497px - that is the deck's own FLOOR,
		 * not just documentation, because `.rb-deck` uses overflow: hidden
		 * and a shorter box genuinely clips hot-cue/transport/jog/pitch/stem
		 * controls (PR #1007 discussion r3921321752 caught this floor being
		 * unenforced: it was only a comment, and the LIBUX-01 reservation
		 * below pushed decks under it at the repo's standard 1280x800
		 * Playwright viewport).
		 *
		 * Sol P1 finding on pin 246b0f5 round 1 (comment 3963232872,
		 * BLOCKING), and FIX ROUND 3's correction to it: `.deck-area`'s
		 * single grid row is ALSO `<Mixer />`'s row, and pin 246b0f5's
		 * `.strip > :not(.fader-slot)` flex-shrink:0 rule means MORE mode's
		 * un-collapsed strip (30px EQs + FILTER, none of it optional here)
		 * needs its own real floor too, not just the deck's 497px. Round 1
		 * "fixed" this by GROWING the shared floor to 524px - but that
		 * violated two separate contracts at once (Sol round-3 BLOCKING
		 * findings, both on this file): at the standard 1280x800/1280x720
		 * viewports it left ~0px for the browser row, letting deck/stem
		 * chrome intercept clicks meant for track rows (the short-window
		 * contract below requires the shortfall to cost ROWS ONLY, never
		 * overlap); and at 969-995px windows it ate into LIBUX-01's 272px
		 * library budget, breaking the five-row guarantee the requirement
		 * promises for every window >= 969px tall. Round 3 fixes this the
		 * other way round: ChannelStrip.svelte's MORE-mode margins
		 * (trim-slot/filter-slot/cue-btn/fader-slot/stem-label) were
		 * tightened instead, so the strip's real, un-clipped content now
		 * fits back inside the ORIGINAL 497px two-deck-column floor -
		 * channel-strip-less-floor.test.mjs's "MORE floor" test derives and
		 * proves this from the real CSS (492.6px required, comfortably
		 * under 497). The deck-area floor is therefore back to a single
		 * number - 497px - because the mixer no longer needs more than the
		 * deck column already provides, not because either requirement was
		 * changed.
		 *
		 * LIBUX-01: reserve enough for the library panel (BrowserPanel) to
		 * always show >= 5 song rows, at the taller "cosy" row height (the
		 * floor must cover both densities, and cosy's 30px rows are the
		 * worse case) - 272px = bottom-bar 18 + pane-header 24 +
		 * SuggestNextStrip's own min-height floor 26 + TrackTable's thead
		 * 20 + 5 * 30px cosy rows 150 + a 17px classic-scrollbar-gutter
		 * allowance (table-wrap's default column widths can exceed a short
		 * window, so a horizontal scrollbar is real on platforms without
		 * overlay scrollbars - PR #1007 discussion r3921198996) + the 17px
		 * `.truncated-note` banner, which renders INSIDE `.tt-root` as a
		 * non-shrinking sibling of the scroll area whenever a
		 * whole-collection search hits its 200-row cap, and would otherwise
		 * spend a row's worth of the same budget (PR #1007 discussion
		 * r3923591731).
		 * SuggestNextStrip/RecommendedSection flex-shrink down to their own
		 * floors first, so the deck area gives up height ahead of them on a
		 * short screen - but only down to its own 497px floor.
		 *
		 * HONEST LIMIT, do not read this as "5 rows everywhere": both floors
		 * (497 deck/mixer + 272 library) plus the 200px topbar/wave add up
		 * to 969px, taller than the 800px standard Playwright viewport, so
		 * the two CANNOT both be satisfied there and no reservation value
		 * can make them. The grid track's 497px floor decides that conflict
		 * in the decks'/mixer's favor (protecting already-shipped, clip-prone
		 * controls over the newer, cosmetic library floor). The arithmetic
		 * at 1280x800 is 28 + 172 + 497 = 697, leaving the browser row 103px
		 * against the 272px it wants; the shortfall shows up as fewer
		 * visible rows, and `.list-panel` in BrowserPanel.svelte clips so
		 * that shortfall cannot paint over the bottom bar or off the window
		 * as it did before (PR #1007 discussion r3921443899). The full
		 * 5-row guarantee holds at any window >= 969px tall (this is
		 * exactly REQUIREMENTS.md's LIBUX-01 acceptance line's own 969px/
		 * 103px figures from when it shipped, issue #995 - round 1's 524px
		 * floor had silently moved that threshold to 996px without amending
		 * the requirement, which is what Sol's round-3 P2 finding caught;
		 * round 3 restores the documented number instead of amending it,
		 * because the mixer's real content does not require more than 497
		 * after the margin fix above). This comment is the current source
		 * of truth for the arithmetic. */
		/* Pin 862cd3 MORE mode (default, unchanged from before the pin):
		 * two full deck columns visible, so the wavestack reserves all 4
		 * waverows. The deck-area floor is the deck's own two-deck-column
		 * height (497px) - see the Sol P1/round-3 paragraph above - which,
		 * after round 3's ChannelStrip.svelte margin fix, also comfortably
		 * covers the mixer's own un-collapsed MORE-mode content.
		 * library-min-5-rows.test.mjs pins deckFloorPx >= columnPx (497)
		 * and channel-strip-less-floor.test.mjs's MORE test pins
		 * deckFloorPx >= the real mixer requirement; both must hold against
		 * whichever number is actually larger. */
		grid-template-rows:
			var(--rb-topbar-h)
			calc(4 * (var(--rb-waverow-h) + var(--rb-stemwave-stack-extra, 0px)))
			minmax(
				497px,
				min(
					500px,
					calc(
						100vh - var(--rb-topbar-h) -
							4 * (var(--rb-waverow-h) + var(--rb-stemwave-stack-extra, 0px)) -
							272px
					)
				)
			)
			minmax(0, 1fr);
		/* MIXUX-09: shared deck/mixer hover chrome while deckHoverUi is active. */
		--rb-deck-hover-inset: inset 0 0 0 1px rgba(255, 255, 255, 0.2);
		--rb-deck-hover-bg: color-mix(
			in srgb,
			rgba(255, 255, 255, 0.1) 40%,
			var(--rb-panel-raised, #1a1e25)
		);
		overflow: hidden;
	}

	.perf-root.show-stem-waveforms {
		--rb-stemwave-stack-extra: calc(3 * var(--rb-stemwave-h));
	}

	/* Pin 862cd3 LESS mode: decks 3/4 collapse to 0 height in place (both
	 * WaveformStack's own `.less` rows and the `.deck-col [data-deck='3'/'4']`
	 * max-height rules below), so the OUTER grid's wavestack/deckarea row
	 * floors must shrink to match - otherwise the freed component-level
	 * height stays trapped inside grid tracks sized for the collapsed
	 * content instead of reaching the library row (`minmax(0, 1fr)` below,
	 * unchanged in both modes), which was exactly the bug this fixes: LESS
	 * hid deck 3/4 chrome but the library gained no vertical space.
	 * Wavestack: 2 waverows instead of 4 (only decks 1/2 are ever shown).
	 *
	 * Deck area / mixer floor - pin 246b0f5 (follow-on to 862cd3): this row
	 * is `.deck-area`'s single grid row, so `<Mixer />` shares it with the
	 * deck columns even though the mixer's OWN content never collapses in
	 * LESS (only decks 3/4's channel-strip WIDTH goes to 0 - see Mixer.svelte's
	 * `.strips.less` rule). One deck's own content-tight floor is 248px ("one
	 * deck needs 248px" above); the mixer's compacted LESS channel strip plus
	 * the MORE/LESS toggle, the headphone/crossfader row below it and
	 * `.rb-mixer`'s own 12px of padding/border chrome comes to more than that,
	 * so the mixer - not the deck - still sets this floor:
	 *
	 *   toggle 17 + strip 173 + lower 76 + chrome 12 = 278px
	 *
	 * where "strip" is one ChannelStrip's own natural (un-clipped) LESS
	 * content height and "chrome" is `.rb-mixer`'s `padding: 6px 6px 4px`
	 * (10px vertical) plus the 1px-per-edge border `.rb-panel` supplies
	 * (theme.css) - both count because the app runs box-sizing: border-box
	 * (app.css), so they eat into the flex column's budget rather than growing
	 * the box past it. channel-strip-less-floor.test.mjs derives every one of
	 * those four terms from ChannelStrip.svelte/Mixer.svelte/theme.css's own
	 * source and asserts each against this comment, so a drifted term is a red
	 * test rather than a documented fact about nothing.
	 *
	 * Pin 2917b0eca218 - the maintainer: "in LESS (2 deck) - channel slider and butons
	 * just below it should probably go Left and Right (around) the EQs. Cant
	 * currently see filter in LESS mode." That moved `.strip.less` from a
	 * single flex COLUMN to a three-column grid, so the requirement stopped
	 * being the sum of every child and became the sum of four grid ROWS: the
	 * strip term fell 296 -> 171 and this floor 401 -> 276, with FILTER back in
	 * the layout rather than unmounted. Pin 8cd32a28c36d (issue #4088) widened
	 * STEM separation (+2px strip padding and stem-label margin), so the strip
	 * term is 173 and this floor is 278 while the fader column budget shrinks.
	 * The reclaimed 125px goes to the library
	 * automatically through the `1fr` row below - it needs no rule of its own -
	 * and only bites on windows short enough for the `min(404px, ...)` cap to
	 * land under the old floor; at 1280x800 the cap already governs.
	 *
	 * History worth keeping, because both were bugs this floor exists to stop:
	 * the pre-246b0f5 total of 388px covered only toggle+strip+lower and left
	 * the mixer's own chrome uncounted (Sol P1, comment 3963232874, BLOCKING),
	 * clipping the last ~12px - and `.rb-mixer` clips overflow, so the fader
	 * and its ChannelLevelMeter, last in the column, went first.
	 * Issue #1578 then found the strip term one px short
	 * (295 -> 296): it counted `.ch-num`'s 10px for `.strip-head`'s height, but
	 * `.strip-head` is a flex row shared with the R/M `.cal-controls` group
	 * (#1475) whose real height is 11px, and a flex row's cross-axis size is
	 * its tallest child. Widened rather than hiding `.cal-controls` in LESS:
	 * R/M are real by-ear calibration controls, and hiding a control to fit a
	 * budget removes function instead of fixing the budget.
	 *
	 * library-min-5-rows.test.mjs's LESS-vs-MORE comparison still holds and
	 * gets easier: LESS frees strictly more room to the library than MORE.
	 * The rendered proof for all of it - fader and meter unclipped, FILTER
	 * visible, slider left of the EQs and STEM right of them - is
	 * performance-less-mode-mixer-levels.spec.ts. */
	.perf-root.deck-layout-less {
		grid-template-rows:
			var(--rb-topbar-h)
			calc(2 * (var(--rb-waverow-h) + var(--rb-stemwave-stack-extra, 0px)))
			minmax(
				278px,
				min(
					404px,
					calc(
						100vh - var(--rb-topbar-h) -
							2 * (var(--rb-waverow-h) + var(--rb-stemwave-stack-extra, 0px)) -
							272px
					)
				)
			)
			minmax(0, 1fr);
	}

	/* PERF-UI-01 (issue #2303): at 1280x720 the MORE-mode leftover after
	 * topbar 28 + 4 * 43 waverows 172 + deck floor 497 is ~23px, which
	 * collapses the browser panel so playlist-all-tracks and track-row sit
	 * outside the viewport. Compact the wavestack only (four decks stay
	 * mounted; LESS already has spare height) so leftover is
	 * 720 - 28 - 88 - 497 = 107px. Breakpoint 799px leaves the 1280x800
	 * LIBUX-01 tests on the 43px waverow. Selector
	 * `.perf-root:not(.deck-layout-less)` so library-min-5-rows.test.mjs's
	 * brace-scoped `.perf-root {` / `.perf-root.deck-layout-less {`
	 * regexes stay exact. Compact rows are below the 34px lyric-lane
	 * floor, so WordLane is hidden rather than painting over the next row.
	 * TopBar shows the paired "WF hidden" compact lyric-status chip at this
	 * breakpoint so LYR staying on does not look like lyrics vanished. */
	@media (max-height: 799px) {
		.perf-root:not(.deck-layout-less) {
			--rb-waverow-h: 22px;
		}
		.perf-root:not(.deck-layout-less) :global(.word-lane) {
			display: none;
		}
	}

	/* !important: the inline style="--rb-deck-layout-duration: ...ms" above
	 * carries higher precedence than a plain author rule (inline style is
	 * author-normal at effectively 1,0,0,0 specificity), so only an
	 * author-!important declaration - this one - can still force 0ms here. */
	@media (prefers-reduced-motion: reduce) {
		.perf-root {
			--rb-deck-layout-duration: 0ms !important;
		}
	}

	/* deck1/deck3 stacked left, mixer center, deck2/deck4 stacked right. */
	.deck-area {
		grid-area: deckarea;
		display: grid;
		grid-template-areas: 'decks-left mixer decks-right';
		grid-template-columns: minmax(0, 1fr) 248px minmax(0, 1fr);
		min-height: 0;
		column-gap: 3px;
		background: #0a0c10;
	}
	.deck-col {
		display: flex;
		flex-direction: column;
		gap: 1px;
		min-height: 0;
		background: #0a0c10;
	}
	.deck-col:first-child {
		grid-area: decks-left;
		border-right: 1px solid #3d4652;
		padding-right: 0;
	}
	.deck-col:last-child {
		grid-area: decks-right;
		border-left: 1px solid #3d4652;
		padding-left: 0;
	}

	/* Pin 862cd3: deck 3/4's own column slot collapses in LESS - chrome
	 * only. <Deck deckId={3}/> and <Deck deckId={4}/> above stay mounted
	 * unconditionally; this only clips their rendered box to 0 height via
	 * their own [data-deck] root (Deck.svelte already sets that attribute
	 * and already uses overflow: hidden - see technically-working-hotkeys.ts'
	 * EDGE_REGION_SELECTOR comment for the same convention). The always-on
	 * 600px ceiling (deck 1/2 never match this selector, so they are
	 * unaffected) is what gives max-height something finite to transition
	 * from - transitioning from an unconstrained natural height does not
	 * animate. 600px is comfortably above the deck's documented ~248px
	 * floor, so MORE mode's rendered height is unchanged. */
	.deck-col :global([data-deck='3']),
	.deck-col :global([data-deck='4']) {
		max-height: 600px;
		overflow: hidden;
		transition:
			max-height var(--rb-deck-layout-duration, 200ms) ease,
			opacity var(--rb-deck-layout-duration, 200ms) ease;
	}
	.perf-root.deck-layout-less .deck-col :global([data-deck='3']),
	.perf-root.deck-layout-less .deck-col :global([data-deck='4']) {
		max-height: 0;
		opacity: 0;
		pointer-events: none;
	}

	/* LIBUX-05 "Technically-working mode": each region hides behind
	 * opacity + pointer-events (never a wrapper around a child component's
	 * own grid-area root - see technically-working.svelte.ts for why) until
	 * its edge is hovered, Opt is held for the "no obvious home" group, or a
	 * peek/inactive state forces everything back. Grid placement is
	 * untouched, so this never risks the deck/mixer/browser layout above.
	 *
	 * The grid CONTAINERS (.perf-root, .deck-area) paint their own opaque
	 * background regardless of which children are visible, so opacity alone
	 * on the children left a solid rectangle over the whole window even with
	 * every region hidden - the "can DJ whilst working" vibe needs the empty
	 * space to actually go transparent and click-through, not just its
	 * content. Revealed regions keep their own panel backgrounds (unchanged
	 * below) so they stay readable; only the gaps around them clear. */
	.perf-root.tw-active {
		background: transparent;
		pointer-events: none;
	}
	.perf-root.tw-active .deck-area,
	.perf-root.tw-active .deck-col {
		background: transparent;
	}
	.perf-root.tw-active :global(.rb-browser),
	.perf-root.tw-active :global(.rb-topbar),
	.perf-root.tw-active :global(.rb-mixer) {
		opacity: 0;
		pointer-events: none;
		transition: opacity 180ms ease-out;
	}
	.perf-root.tw-active.tw-no-animate :global(.rb-browser),
	.perf-root.tw-active.tw-no-animate :global(.rb-topbar),
	.perf-root.tw-active.tw-no-animate :global(.rb-mixer) {
		transition: none;
	}
	/* Per-deck, not per-edge (LIBUX-05: "only for the decks actually in
	 * use") - .deck-col/.rb-wavestack own no visibility of their own
	 * anymore, each [data-deck] child (Deck.svelte's own root, WaveRow's own
	 * root) hides or reveals independently so a hover only surfaces the
	 * deck(s) actually loaded, not its unloaded column-mate or an empty
	 * waveform row. */
	.perf-root.tw-active .deck-col :global([data-deck]),
	.perf-root.tw-active :global(.rb-wavestack [data-deck]) {
		opacity: 0;
		pointer-events: none;
		transition: opacity 180ms ease-out;
	}
	.perf-root.tw-active.tw-no-animate .deck-col :global([data-deck]),
	.perf-root.tw-active.tw-no-animate :global(.rb-wavestack [data-deck]) {
		transition: none;
	}
	.perf-root.tw-active.tw-deck1-visible .deck-col :global([data-deck='1']),
	.perf-root.tw-active.tw-deck2-visible .deck-col :global([data-deck='2']),
	.perf-root.tw-active.tw-deck3-visible .deck-col :global([data-deck='3']),
	.perf-root.tw-active.tw-deck4-visible .deck-col :global([data-deck='4']),
	.perf-root.tw-active.tw-wave1-visible :global(.rb-wavestack [data-deck='1']),
	.perf-root.tw-active.tw-wave2-visible :global(.rb-wavestack [data-deck='2']),
	.perf-root.tw-active.tw-wave3-visible :global(.rb-wavestack [data-deck='3']),
	.perf-root.tw-active.tw-wave4-visible :global(.rb-wavestack [data-deck='4']) {
		opacity: 1;
		pointer-events: auto;
	}
	.perf-root.tw-active.tw-bottom-visible :global(.rb-browser) {
		opacity: 1;
		pointer-events: auto;
	}
	.perf-root.tw-active.tw-homeless-visible :global(.rb-topbar),
	.perf-root.tw-active.tw-homeless-visible :global(.rb-mixer) {
		opacity: 1;
		pointer-events: auto;
	}
	/* Opt-held reveal reads over a full translucent backdrop so the homeless
	 * group stays readable against whatever else is still hidden underneath.
	 * Gated on tw-opt-reveal specifically, never tw-homeless-visible: that
	 * class is ALSO true while peeking (cmd+R held), where every region is
	 * already fully visible (isDeckSlotVisible/isEdgeVisible both return true
	 * while peeking) - a backdrop there would dim the very content peek
	 * exists to bring back into full view. */
	.perf-root.tw-active.tw-opt-reveal::before {
		content: '';
		position: fixed;
		inset: 0;
		z-index: 30;
		background: rgba(0, 0, 0, 0.55);
		pointer-events: none;
	}
	.perf-root.tw-active :global(.rb-topbar),
	.perf-root.tw-active :global(.rb-mixer) {
		position: relative;
		z-index: 31;
	}
</style>
