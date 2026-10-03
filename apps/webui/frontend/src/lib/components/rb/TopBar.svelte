<script lang="ts">
	/**
	 * Build unit: topbar (COMPONENT-MAP 1.1, SCREENSHOT-SPEC 1).
	 * Real elements: master-volume slider (webaudio master GainNode) and
	 * the live clock (client Date, HH:MM). Everything else renders
	 * visually authentic but inert per the component map: PERFORMANCE
	 * dropdown, view-layout icon cluster (4-waveform icon painted
	 * active/blue statically), LINK, PAD/MIDI dim labels, info icon,
	 * gear, refresh arrow. The yellow Free badge is static chrome.
	 *
	 * Master volume renders the shared mixer read model and dispatches through
	 * the same typed command path used by browser IPC and presets.
	 */
	import { page } from '$app/stores';
	import { onMount, tick } from 'svelte';
	import { clampToViewport } from '$lib/ui/clamp-to-viewport';
	import { DECK_IDS, engine, getDeckState, isMasterMuted, mixerState } from '$lib/rb/audio-engine.svelte';
	import { masterMuteReason } from '$lib/player/master-mute.svelte';
	import { openStage } from '$lib/lyrics/stage-store.svelte';
	import type { StageDeck } from '$lib/lyrics/stage-store.svelte';
	import { anyDeckPlaying } from '$lib/rb/playing-gate';
	import type { AudioEngine } from '$lib/rb/audio-engine-types';
	import {
		dispatchPerformanceCommand,
		runPerformanceCommandFromUi,
		type PairingSnapshot
	} from '$lib/rb/performance-ipc.svelte';
	import { autoPlayNextState } from '$lib/rb/auto-play-next.svelte';
	import {
		setAutoPlayEnabled,
		setAutoPlayEnforceOrder,
		setAppMode,
		setAutoPlayMaximizeReach,
		setBeatSyncMax,
		setDeckLayoutMode,
		describeTwoDeckToggle,
		toggleLyricsGlobal,
		toggleTheme,
		uiPrefs
	} from '$lib/rb/prefs.svelte';
	import { describeAutoPlayMode } from '$lib/rb/autoplay-mode';
	import { openSettings } from '$lib/settings/hotkeys';
	import { vibeState } from '$lib/rb/vibe.svelte';
	import { WHEEL_STEP, wheelAdjust } from '$lib/rb/wheel-adjust';
	import { audioOutputHealth } from '$lib/rb/audio-output-health.svelte';
	import { describeAudioOutputHealth } from '$lib/rb/audio-output-health-display';
	import { switchDeviceOutput } from '$lib/rb/device-output-probe-control';

	const outputHealthDisplay = $derived(describeAudioOutputHealth(audioOutputHealth.snapshot));
	const listViewBullets = plannedExplainerBullets('list-view');
	const fxBullets = plannedExplainerBullets('fx');
	const twoDeck = $derived(describeTwoDeckToggle(uiPrefs.deck_layout));
	const fourWaveformBullets = plannedExplainerBullets('4-waveform-view');
	const scopeView1Bullets = plannedExplainerBullets('scope-view-1');
	const scopeView2Bullets = plannedExplainerBullets('scope-view-2');
	const linkBullets = [
		...plannedExplainerBullets('link'),
		'When built, tempo and phase align across laptops on the same network; this button joins or leaves that session.'
	];
	let switchOutputBusy = $state(false);

	async function handleSwitchOutput(): Promise<void> {
		if (switchOutputBusy || !outputHealthDisplay.switchOutputAvailable) return;
		if (
			!confirm(
				'Switch the macOS default output to another device and back? This is the same fix as choosing a different output in System Settings.'
			)
		) {
			return;
		}
		switchOutputBusy = true;
		try {
			await switchDeviceOutput();
		} finally {
			switchOutputBusy = false;
		}
	}
	import UserBauble from '$lib/components/UserBauble.svelte';
	import AppPostureChip from './AppPostureChip.svelte';
	import GigHelperMonitor from './GigHelperMonitor.svelte';
	import GigHelperPrompt from './GigHelperPrompt.svelte';
	import AnalysisSourceToggle from './AnalysisSourceToggle.svelte';
	import CloudSyncStatusChip from '$lib/components/CloudSyncStatusChip.svelte';
	import CommandEntry from './CommandEntry.svelte';
	import FeedbackWidget from './FeedbackWidget.svelte';
	import PerfMeters from './PerfMeters.svelte';
	import { plannedExplainerBullets, plannedTitle } from '$lib/rb/planned-explainers';
	import ControlExplainer from './deck/ControlExplainer.svelte';
	import StemsProgress from './StemsProgress.svelte';
	import VibeMeter from './VibeMeter.svelte';
	import TransitioningChip from './TransitioningChip.svelte';
	import JobsDrawer from '$lib/components/rb/JobsDrawer.svelte';
	import { jobsRefusal } from '$lib/api/capabilities.svelte';
	import { jobsStore, toggleJobsDrawer } from '$lib/rb/jobs-store.svelte';
	import MidiPanel from '$lib/components/rb/MidiPanel.svelte';
	import MidiLearnLogPopout from '$lib/components/rb/midi/MidiLearnLogPopout.svelte';
	import { midiLabelGlyph, midiLabelStatus, midiLabelTitle } from '$lib/components/rb/midi/midi-format';
	import { maybeAutoEnableMidi, midiUi, toggleMidiPanel } from '$lib/components/rb/midi/midi-ui-state.svelte';
	import { midiState } from '$lib/rb/midi/webmidi.svelte';
	import RefreshAnalysisButton from './RefreshAnalysisButton.svelte';
	import MasterLevelMeter from './mixer/MasterLevelMeter.svelte';
	import {
		APP_MODES,
		LOCAL_STEMS_EXECUTOR_FLAG_ID,
		appModeForPath,
		modeFeatureEnabled
	} from '$lib/rb/app-mode';
	import { modeIconClass } from '$lib/rb/app-mode-icons';
	import { markLibraryModeExit } from '$lib/rb/library-mode-runtime';

	interface MasterCapableEngine extends AudioEngine {
		setMaster(value: number): void;
	}

	/** Why the JOBS toggle is inert, or null when the daemon offers jobs. A
	 * different category from the planned-explainer catalogue: those describe
	 * a feature nobody has built, this one is built and simply not served by
	 * this daemon. Two different sentences, deliberately not merged. */
	const jobsUnavailable = $derived(jobsRefusal());

	let pairingOpen = $state(false);
	let pairingSnapshot = $state<PairingSnapshot | null>(null);
	/** The capture sheet loads on its first open, so it stays out of the
	 * /performance route's initial bundle (budget in scripts/bundle-budget.mjs). */
	let PairingSheet = $state<typeof import('./CreatePairingSheet.svelte').default | null>(null);
	let autoPlayMenuOpen = $state(false);
	let autoPlayWrapEl: HTMLSpanElement | undefined = $state();
	let autoPlayMenuStyle = $state('');
	let autoPlayMenuEl: HTMLDivElement | undefined = $state();
	let modePickerEl: HTMLDetailsElement | undefined = $state();
	let modeMenuStyle = $state('');
	let modeMenuEl: HTMLDivElement | undefined = $state();

	const liveAppMode = APP_MODES.find((mode) => mode.id === 'performance');
	if (liveAppMode === undefined) {
		throw new Error('APP_MODES is missing the live /performance route');
	}

	const activeMode = $derived.by(() => {
		try {
			return appModeForPath($page.url.pathname);
		} catch {
			return APP_MODES.find((mode) => mode.id === uiPrefs.app_mode) ?? liveAppMode;
		}
	});

	const modePickerTitle = $derived(
		`App mode picker - ${activeMode.label} is the current mode`
	);

	const stemsProgressLive = $derived(
		modeFeatureEnabled(liveAppMode.id, LOCAL_STEMS_EXECUTOR_FLAG_ID)
	);

	function _selectAppMode(modeId: (typeof APP_MODES)[number]['id']): void {
		if (modeId === 'library') markLibraryModeExit();
		setAppMode(modeId);
	}

	const autoPlayTitle: string = $derived.by(() => {
		const d = describeAutoPlayMode(uiPrefs);
		if (d.mode === 'off') return `${d.short} - ${d.detail}`;
		return `AutoPlay ON (${d.short}) - last ~16s loads onto a free/stopped deck. ${d.detail}`;
	});

	// Pin fc60002b81a8: ">|" split of the AutoPlay button, early next-track
	// transition trigger. Toggles arm/cancel through the same command path
	// as every other performance control (see auto-play-next.svelte.ts).
	function _toggleAutoPlayNext(): void {
		void runPerformanceCommandFromUi(
			autoPlayNextState.armed ? { type: 'auto_play_next_cancel' } : { type: 'auto_play_next_arm' }
		);
	}

	async function _placeAutoPlayMenu(): Promise<void> {
		if (!autoPlayWrapEl) return;
		const r = autoPlayWrapEl.getBoundingClientRect();
		await tick();
		const menuRect = autoPlayMenuEl?.getBoundingClientRect() ?? { width: 220, height: 120 };
		const box = clampToViewport(
			r.right,
			r.bottom + 6,
			{ width: menuRect.width, height: menuRect.height },
			{ width: window.innerWidth, height: window.innerHeight }
		);
		autoPlayMenuStyle = `left:${Math.round(box.x)}px;top:${Math.round(box.y)}px`;
	}

	function _showAutoPlayMenu(): void {
		autoPlayMenuOpen = true;
		void _placeAutoPlayMenu();
	}

	function _hideAutoPlayMenu(e: FocusEvent | PointerEvent): void {
		const next =
			e instanceof FocusEvent
				? e.relatedTarget
				: (e as PointerEvent).relatedTarget;
		if (next instanceof Node && autoPlayWrapEl?.contains(next)) return;
		// Fixed menu is outside the wrap - keep open when moving into it.
		if (next instanceof Element && next.closest?.('.ap-menu')) return;
		autoPlayMenuOpen = false;
	}

	async function _placeModeMenu(): Promise<void> {
		if (!modePickerEl?.open) return;
		const rect = modePickerEl.getBoundingClientRect();
		await tick();
		const menuRect = modeMenuEl?.getBoundingClientRect() ?? { width: 220, height: 180 };
		const box = clampToViewport(
			rect.left,
			rect.bottom + 5,
			{ width: menuRect.width, height: menuRect.height },
			{ width: window.innerWidth, height: window.innerHeight }
		);
		modeMenuStyle = `left:${Math.round(box.x)}px;top:${Math.round(box.y)}px`;
	}

	function _dismissModeMenuOnOutsidePointer(e: PointerEvent): void {
		if (!modePickerEl?.open) return;
		if (e.target instanceof Node && modePickerEl.contains(e.target)) return;
		modePickerEl.open = false;
	}

	function _dismissModeMenuOnEscape(e: KeyboardEvent): void {
		if (e.key !== 'Escape' || !modePickerEl?.open) return;
		modePickerEl.open = false;
	}

	/** Native `toggle` on `<details>` is wired with a `use:` action; Svelte 5
	 * does not accept `ontoggle` as a template handler on this element. */
	function modePickerToggle(node: HTMLDetailsElement): { destroy: () => void } {
		const onToggle = (): void => {
			void _placeModeMenu();
		};
		node.addEventListener('toggle', onToggle);
		return {
			destroy: () => {
				node.removeEventListener('toggle', onToggle);
			}
		};
	}

	/** 4-waveform view icon geometry: 4 stacked jagged polylines (one per
	 * deck row) so the glyph reads as 4 waveforms, not a dotted grid. */
	const WAVE_ICON_XS: number[] = [1, 3, 5, 7, 9, 11, 13];
	const WAVE_ICON_ROWS: { cy: number; offsets: number[] }[] = [
		{ cy: 1.6, offsets: [0.3, -0.9, 0.6, -0.4, 0.9, -0.2, 0.4] },
		{ cy: 4.4, offsets: [-0.5, 0.9, -0.9, 0.5, -0.3, 0.8, -0.6] },
		{ cy: 7.2, offsets: [0.6, -0.3, 0.9, -0.7, 0.3, -0.9, 0.5] },
		{ cy: 10, offsets: [-0.3, 0.5, -0.7, 0.3, -0.6, 0.4, -0.2] }
	];

	function _waveIconPoints(row: { cy: number; offsets: number[] }): string {
		return WAVE_ICON_XS.map((x, i) => `${x},${row.cy + row.offsets[i]}`).join(' ');
	}

	/** Gear (settings) icon: 8 square teeth radiating off the ring so the
	 * glyph reads as a mechanical cog, not a sun with rays. */
	const GEAR_TOOTH_ANGLES: number[] = [0, 45, 90, 135, 180, 225, 270, 315];

	let clock = $state(_formatClock(new Date()));
	let masterDragging = false;

	/** MIDI label status (build unit: midi panel): grey = unsupported /
	 * denied / idle, amber pulse = permission prompt pending, green = at
	 * least one mapped device connected. Logic lives in midi-format.ts
	 * (pure, unit-tested); this is just the reactive plumbing. */
	const midiMappedCount = $derived(
		midiState.devices.filter((d) => d.mapVendor !== null).length
	);
	const midiStatus = $derived(
		midiLabelStatus(midiState.permission, midiUi.requestPending, midiMappedCount > 0)
	);
	const midiGlyph = $derived(midiLabelGlyph(midiStatus));
	const midiTitle = $derived(
		midiLabelTitle(midiState.permission, midiUi.requestPending, midiMappedCount, midiState.devices.length)
	);

	// Re-run the access request on load IFF the user opted in before (persisted
	// choice). Goes through requestMidiAccess() - the single init trigger that
	// also registers device maps + attaches the glue - so the invariant holds.
	onMount(() => {
		void maybeAutoEnableMidi();
	});

	$effect(() => {
		const id = setInterval(() => {
			clock = _formatClock(new Date());
		}, 1000);
		return () => clearInterval(id);
	});

	function _formatClock(d: Date): string {
		return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
	}

	function _clamp01(v: number): number {
		return Math.min(1, Math.max(0, v));
	}

	function _setMaster(value: number): void {
		void runPerformanceCommandFromUi({ type: 'master_volume', value });
	}

	async function _openPairing(): Promise<void> {
		PairingSheet ??= (await import('./CreatePairingSheet.svelte')).default;
		const state = await dispatchPerformanceCommand({ type: 'pairing_snapshot_open' });
		if (state.pairing_snapshot === null) throw new Error('pairing snapshot was not captured');
		pairingSnapshot = state.pairing_snapshot;
		pairingOpen = true;
	}

	function _masterFromEvent(e: PointerEvent): number {
		const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
		return _clamp01((e.clientX - rect.left) / rect.width);
	}

	function handleMasterDown(e: PointerEvent): void {
		masterDragging = true;
		(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
		_setMaster(_masterFromEvent(e));
	}

	function handleMasterMove(e: PointerEvent): void {
		if (!masterDragging) return;
		_setMaster(_masterFromEvent(e));
	}

	function handleMasterUp(e: PointerEvent): void {
		if (!masterDragging) return;
		masterDragging = false;
		(e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
	}

	function handleMasterKeyDown(e: KeyboardEvent): void {
		if (e.key === 'ArrowRight' || e.key === 'ArrowUp') {
			e.preventDefault();
			_setMaster(_clamp01(mixerState.master + 0.02));
		} else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') {
			e.preventDefault();
			_setMaster(_clamp01(mixerState.master - 0.02));
		}
	}

	// TopBar is mounted for the whole /performance session, so the master
	// meter's RAF loop must not run unconditionally from route mount - it
	// stops the instant nothing is playing, same gate as
	// armAudioContextWatchdog's arm predicate (anyDeckPlaying is the one
	// shared source of truth for "is this session live").
	const masterMeterActive = $derived(anyDeckPlaying());

	/** Stage overlay target: first playing/audible loaded deck, else first loaded deck. */
	const stageTarget = $derived.by((): { deck: StageDeck; stableId: string } | null => {
		for (const deck of DECK_IDS) {
			const st = getDeckState(deck);
			if (st.stable_id !== null && (st.playing || st.audible)) {
				return { deck: deck as StageDeck, stableId: st.stable_id };
			}
		}
		for (const deck of DECK_IDS) {
			const st = getDeckState(deck);
			if (st.stable_id !== null) {
				return { deck: deck as StageDeck, stableId: st.stable_id };
			}
		}
		return null;
	});

	const stageTitle = $derived(
		stageTarget === null
			? 'Stage - load a track onto a deck first'
			: `Open full-screen karaoke stage for deck ${stageTarget.deck}`
	);

	function _openStageFromTopBar(): void {
		if (stageTarget === null) return;
		openStage(stageTarget.stableId, stageTarget.deck);
	}
</script>

<svelte:window onpointerdown={_dismissModeMenuOnOutsidePointer} onkeydown={_dismissModeMenuOnEscape} />

<header
	class="rb-topbar rb-panel"
	class:vibe-rainbow={vibeState.display >= 0.9}
	style={vibeState.display >= 0.9 ? `--vr:${vibeState.rainbow_index}` : undefined}
>	<!-- top-left: PARITY-02 rbx-vs-own source A/B toggle (issue #1002),
	     ahead of the live audio health + prefetch count and mode dropdown. -->
	<AnalysisSourceToggle />

	<PerfMeters />

	<!-- Stems separation, aggregate and live off jobs.updated. Renders nothing
	     while no stems job is active, so it costs no space the rest of the
	     time; clicking it opens the JOBS drawer for the per-job detail. -->
	{#if stemsProgressLive}
		<StemsProgress />
	{/if}

	<details class="mode-picker" bind:this={modePickerEl} use:modePickerToggle>
		<summary class="mode-dd" aria-label="Choose app mode" title={modePickerTitle}>
			{activeMode.label.toUpperCase()}
			<svg width="7" height="5" viewBox="0 0 7 5" aria-hidden="true">
				<path d="M0.5 1 L3.5 4 L6.5 1" fill="none" stroke="currentColor" stroke-width="1.2" />
			</svg>
		</summary>
		<div class="mode-menu" bind:this={modeMenuEl} style={modeMenuStyle} aria-label="App modes">
			<p class="mode-menu-heading">Choose app mode</p>
			{#each APP_MODES as mode (mode.id)}
				<a
					class="mode-card"
					data-testid="mode-card"
					href={mode.href}
					aria-current={mode.id === activeMode.id ? 'page' : undefined}
					onclick={() => _selectAppMode(mode.id)}
				>
					<span class={`mode-thumbnail ${modeIconClass(mode.iconId)}`} aria-hidden="true"></span>
					<span class="mode-copy">
						<strong>{mode.label}</strong>
						<span class="mode-gain" data-testid="mode-gain">{mode.gain}</span>
						<span class="mode-lose" data-testid="mode-lose">{mode.lose}</span>
					</span>
				</a>
			{/each}
		</div>
	</details>
	<AppPostureChip />
	<GigHelperMonitor />
	<GigHelperPrompt />

	<div class="icon-cluster">
		<!-- list-view icon with dropdown caret -->
		<ControlExplainer title="List view" bullets={listViewBullets} showDelayMs={60}>
			<button class="tb-icon rb-inert" disabled aria-label="list view">
				<svg width="16" height="12" viewBox="0 0 16 12" aria-hidden="true">
					<rect x="1" y="1.5" width="9" height="1.6" fill="currentColor" />
					<rect x="1" y="5.2" width="9" height="1.6" fill="currentColor" />
					<rect x="1" y="8.9" width="9" height="1.6" fill="currentColor" />
					<path d="M11.5 5 L13.5 7 L15.5 5" fill="none" stroke="currentColor" stroke-width="1.1" />
				</svg>
			</button>
		</ControlExplainer>
		<!-- FX panel toggle -->
		<ControlExplainer title="FX panel" bullets={fxBullets} demo="fx" showDelayMs={60}>
			<button class="tb-icon fx rb-inert" disabled aria-label="FX panel">FX</button>
		</ControlExplainer>
		<!-- Split view and grid view are unbuilt: HIDDEN for V1 rather than shown
		     disabled (JIK, Thu 1 Oct 2026). Their planned-explainers entries stay
		     so the copy is ready when they are built. -->
		<!-- 2up icon: a real toggle between the 2-deck (LESS) and 4-deck (MORE)
		     layouts, via the same setDeckLayoutMode path Cmd/Ctrl+2 and
		     Cmd/Ctrl+4 use (deck-layout-hotkeys.ts) - see two-deck-toggle.ts. -->
		<ControlExplainer title="2-deck view" demo="2-deck-view" showDelayMs={60}>
			<button
				type="button"
				class="tb-icon"
				class:active={twoDeck.pressed}
				aria-label="2 deck view"
				aria-pressed={twoDeck.pressed}
				aria-keyshortcuts="Meta+2 Control+2 Meta+4 Control+4"
				title={twoDeck.title}
				onclick={() => setDeckLayoutMode(twoDeck.next)}
			>
				<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
					<rect x="1" y="2" width="4.4" height="8" fill="none" stroke="currentColor" />
					<rect x="6.6" y="2" width="4.4" height="8" fill="none" stroke="currentColor" />
				</svg>
			</button>
		</ControlExplainer>
		<!-- 4-waveform icon: the ACTIVE layout, painted blue statically. Four
		     stacked jagged polylines - must NOT read as a plain list glyph or
		     a dotted grid (SCREENSHOT-SPEC 1). -->
		<ControlExplainer title="4-waveform view" bullets={fourWaveformBullets} showDelayMs={60}>
			<button class="tb-icon active rb-inert" disabled aria-label="4 waveform view">
				<svg width="14" height="12" viewBox="0 0 14 12" aria-hidden="true">
					{#each WAVE_ICON_ROWS as row (row.cy)}
						<polyline
							points={_waveIconPoints(row)}
							fill="none"
							stroke="currentColor"
							stroke-width="0.9"
							stroke-linecap="round"
							stroke-linejoin="round"
						/>
					{/each}
				</svg>
			</button>
		</ControlExplainer>
		<!-- 2 circular scope icons -->
		<ControlExplainer title="Phase scope" bullets={scopeView1Bullets} showDelayMs={60}>
			<button class="tb-icon rb-inert" disabled aria-label="scope view 1">
				<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
					<circle cx="6" cy="6" r="4.6" fill="none" stroke="currentColor" />
					<circle cx="6" cy="6" r="1.4" fill="currentColor" />
				</svg>
			</button>
		</ControlExplainer>
		<ControlExplainer title="Phase meter" bullets={scopeView2Bullets} showDelayMs={60}>
			<button class="tb-icon rb-inert" disabled aria-label="scope view 2">
				<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
					<circle cx="6" cy="6" r="4.6" fill="none" stroke="currentColor" />
					<path d="M6 1.4 A4.6 4.6 0 0 1 10.6 6" fill="none" stroke="currentColor" stroke-width="1.6" />
				</svg>
			</button>
		</ControlExplainer>
	</div>

	<!-- center-left: LINK, given clear room from the left icon cluster so it
	     reads as centre-left rather than butted against the left group
	     (SCREENSHOT-SPEC 1). -->
	<div class="spacer-left"></div>

	<ControlExplainer title="LINK" bullets={linkBullets} demo="link" showDelayMs={60}>
		<button class="link-btn rb-inert" disabled aria-label="LINK">LINK</button>
	</ControlExplainer>

	<div class="spacer"></div>

	<!-- review/feedback widget: chevron + comment pin on the LEFT-hand side of
	     the vibe meter (FB-07, superseding the right-hand placement shipped in
	     #523); REAL -> /api/v1/feedback, inert when the daemon does not serve
	     it (FB-01..FB-04). Both this and the vibe meter are absolutely
	     centred, so DOM order does not place them - the .fb-cluster anchor
	     does. It reads first here so focus and screen-reader order match what
	     is on screen. -->
	<FeedbackWidget />

	<!-- dead-center vibe meter: mouse movement tops it up; history in localStorage -->
	<div class="vibe-slot topbar-slot-vibe">
		<VibeMeter />
	</div>

	<TransitioningChip />

	<!-- right cluster -->
	<button
		type="button"
		class="bsm-toggle topbar-slot-pairing"
		title="Create pairing from two decks"
		aria-label="Create pairing"
		onclick={() => void _openPairing()}
	>
		Create pairing
	</button>

	<button
		type="button"
		class="bsm-toggle topbar-slot-bsm"
		class:on={uiPrefs.beat_sync_max}
		aria-pressed={uiPrefs.beat_sync_max}
		title={uiPrefs.beat_sync_max
			? 'BeatSyncMax ON - downbeats stay aligned; every seek (incl. master) keeps BAR phase lock'
			: 'BeatSyncMax OFF - followers sync on seek using their own BEAT/BAR mode; master free-seeks'}
		onclick={() => setBeatSyncMax(!uiPrefs.beat_sync_max)}
	>
		BeatSyncMax
	</button>

	<button
		type="button"
		class="bsm-toggle topbar-slot-lyr"
		class:on={uiPrefs.lyrics_global}
		aria-pressed={uiPrefs.lyrics_global}
		title={uiPrefs.lyrics_global
			? 'Lyric overlays ON - click to hide waveform word lanes, deck lyric lines and scrub-hover words everywhere (per-surface toggles keep their state)'
			: 'Lyric overlays OFF globally - click to restore them (library Lyrics column is unaffected; it has its own setting)'}
		onclick={toggleLyricsGlobal}
	>
		LYR
	</button>
	<span
		class="lyrics-compact-chip topbar-slot-lyr-compact"
		title="Waveform word lane hidden at this viewport height - LYR preference stays on; use Stage for full-screen lyrics"
		aria-label="Waveform lyrics compact - word lane hidden at this height"
	>
		WF hidden
	</span>

	<button
		type="button"
		class="bsm-toggle topbar-slot-stage"
		disabled={stageTarget === null}
		aria-label="Open karaoke stage"
		title={stageTitle}
		onclick={_openStageFromTopBar}
	>
		Stage
	</button>

	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<span
		class="ap-wrap topbar-slot-autoplay"
		class:on={uiPrefs.auto_play_enabled || autoPlayNextState.armed}
		bind:this={autoPlayWrapEl}
		onpointerenter={_showAutoPlayMenu}
		onpointerleave={_hideAutoPlayMenu}
		onfocusin={_showAutoPlayMenu}
		onfocusout={_hideAutoPlayMenu}
	>
		<button
			type="button"
			class="bsm-toggle"
			class:on={uiPrefs.auto_play_enabled}
			aria-pressed={uiPrefs.auto_play_enabled}
			title={autoPlayTitle}
			onclick={() => setAutoPlayEnabled(!uiPrefs.auto_play_enabled)}
		>
			AutoPlay
		</button>
		<button
			type="button"
			class="bsm-toggle ap-next-btn"
			data-rust-command="auto_play_next_arm"
			class:on={uiPrefs.auto_play_enabled || autoPlayNextState.armed}
			aria-pressed={autoPlayNextState.armed}
			title={autoPlayNextState.armed
				? `Next-track loop armed (${autoPlayNextState.phase}) - click to cancel`
				: 'Next-track loop: loop the outgoing track\'s last repetitive 8 beats, duck LOW 30% once the incoming bass enters, cut at the approximate drop'}
			onclick={_toggleAutoPlayNext}
		>
			&gt;|
		</button>
		{#if autoPlayMenuOpen}
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div
				class="ap-menu"
				bind:this={autoPlayMenuEl}
				style={autoPlayMenuStyle}
				role="dialog"
				aria-label="AutoPlay options"
				onpointerenter={_showAutoPlayMenu}
				onpointerleave={() => (autoPlayMenuOpen = false)}
			>
				<p class="ap-head">AutoPlay</p>
				<label class="ap-row">
					<input
						type="checkbox"
						checked={uiPrefs.auto_play_enforce_order}
						onchange={(e) =>
							setAutoPlayEnforceOrder((e.currentTarget as HTMLInputElement).checked)}
					/>
					<span>Enforce play order</span>
				</label>
				<label class="ap-row">
					<input
						type="checkbox"
						checked={uiPrefs.auto_play_maximize_reach}
						disabled={uiPrefs.auto_play_enforce_order}
						onchange={(e) =>
							setAutoPlayMaximizeReach((e.currentTarget as HTMLInputElement).checked)}
					/>
					<span>Maximize reach (avoid stranding)</span>
				</label>
				<button
					type="button"
					class="ap-row ap-two-track rb-inert"
					disabled
					title={plannedTitle('autoplay-two-track')}
				>
					<span>Two-track AutoPlay</span>
					<span class="ap-planned">Planned</span>
				</button>
				<p class="ap-hint">
					Off enforce: key +-1 + Beat Sync BPM. Maximize reach (default on) prefers
					fewer-outward twins so later tracks stay playable. Enforce order: next row
					after current (ignores maximize reach).
				</p>
			</div>
		{/if}
	</span>

	<span class="dim-label topbar-slot-pad" title={plannedTitle('pad')}>PAD</span>
	<!-- MIDI: LIVE (build unit: midi panel) - status colour + panel toggle -->
	<button
		class="midi-label topbar-slot-midi"
		class:st-grey={midiStatus === 'grey'}
		class:st-green={midiStatus === 'green'}
		class:st-amber={midiStatus === 'amber'}
		class:st-red={midiStatus === 'red'}
		title={midiTitle}
		aria-label="MIDI panel"
		aria-expanded={midiUi.panelOpen}
		onclick={toggleMidiPanel}
	>
		MIDI{#if midiGlyph === 'check'}<svg
				class="midi-glyph"
				data-icon="check"
				width="8"
				height="8"
				viewBox="0 0 8 8"
				aria-hidden="true"
				><path
					d="M1 4.2 3.1 6.3 7 1.8"
					fill="none"
					stroke="currentColor"
					stroke-width="1.5"
					stroke-linecap="round"
					stroke-linejoin="round"
				/></svg
			>{:else if midiGlyph === 'cross'}<svg
				class="midi-glyph"
				data-icon="cross"
				width="8"
				height="8"
				viewBox="0 0 8 8"
				aria-hidden="true"
				><path
					d="M1.6 1.6 6.4 6.4M6.4 1.6 1.6 6.4"
					fill="none"
					stroke="currentColor"
					stroke-width="1.5"
					stroke-linecap="round"
				/></svg
			>{/if}
	</button>

	<!-- JOBS: LIVE (build unit: T5 jobs) - engine job list, opens the drawer.
	     Inert on a daemon with no jobs API, and the title says which. -->
	<button
		class="midi-label topbar-slot-jobs"
		class:rb-inert={jobsUnavailable !== null}
		disabled={jobsUnavailable !== null}
		title={jobsUnavailable ??
			'Engine jobs: what the daemon is running right now (live over the jobs.updated topic)'}
		aria-label="Jobs drawer"
		aria-expanded={jobsStore.drawerOpen}
		onclick={toggleJobsDrawer}
	>
		JOBS
	</button>

	<!-- text-command entry: closest rekordbox-parity hook for apps/voice
	     (no mic UI in rekordbox); REAL -> POST /api/v1/voice/probe -->
	<CommandEntry />

	<button class="tb-icon rb-inert topbar-slot-utility" disabled title={plannedTitle('information')} aria-label="information">
		<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
			<circle cx="6" cy="6" r="5" fill="none" stroke="currentColor" />
			<rect x="5.3" y="5" width="1.4" height="4" fill="currentColor" />
			<rect x="5.3" y="2.6" width="1.4" height="1.4" fill="currentColor" />
		</svg>
	</button>

	<span class="free-badge" title={plannedTitle('free-badge')}>Free</span>

	<button
		type="button"
		class="tb-icon theme-toggle topbar-slot-utility"
		class:on={uiPrefs.theme === 'light'}
		title={uiPrefs.theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
		aria-label={uiPrefs.theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
		onclick={toggleTheme}
	>
		{#if uiPrefs.theme === 'dark'}
			<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="2.4" fill="none" stroke="currentColor" stroke-width="1.2" /><path d="M6 0.8v1.3M6 9.9v1.3M0.8 6h1.3M9.9 6h1.3M2.3 2.3l.9.9M8.8 8.8l.9.9M9.7 2.3l-.9.9M3.2 8.8l-.9.9" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" /></svg>
		{:else}
			<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M9.8 8.1A4.5 4.5 0 0 1 3.9 2.2 4.6 4.6 0 1 0 9.8 8.1Z" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round" /></svg>
		{/if}
	</button>

	<button
		type="button"
		class="tb-icon theme-toggle topbar-slot-utility"
		title="Settings (Cmd+,)"
		aria-label="Open settings"
		onclick={() => openSettings()}
	>
		<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
			<circle cx="6" cy="6" r="2.1" fill="none" stroke="currentColor" stroke-width="1.3" />
			{#each GEAR_TOOTH_ANGLES as angle (angle)}
				<rect
					x="5.15"
					y="0.6"
					width="1.7"
					height="1.7"
					fill="currentColor"
					transform={`rotate(${angle} 6 6)`}
				/>
			{/each}
		</svg>
	</button>

	<!-- refresh analysis: REAL -> /api/v1/ingest refresh job (was inert chrome) -->
	<RefreshAnalysisButton />

	<!-- master volume: REAL -> engine master GainNode -->
	<div class="master-slider-wrap">
		<div
			class="master-slider"
			role="slider"
			aria-label="master volume"
			title="Master volume - final output gain"
			aria-orientation="horizontal"
			aria-valuemin={0}
			aria-valuemax={1}
			aria-valuenow={mixerState.master}
			tabindex="0"
			use:wheelAdjust={{
				step: WHEEL_STEP.fader,
				get: () => mixerState.master,
				set: _setMaster
			}}
			onpointerdown={handleMasterDown}
			onpointermove={handleMasterMove}
			onpointerup={handleMasterUp}
			onkeydown={handleMasterKeyDown}
		>
			<div class="master-track"></div>
			<div class="master-fill" style={`width: ${mixerState.master * 100}%;`}></div>
			<div class="master-thumb" style={`left: calc(${mixerState.master * 100}% - 4px);`}></div>
		</div>

		<!-- master output level meter: REAL -> engine master bus, post master
		     gain (pin 5a5c3b8033d8's still-open half; the ten-segment channel
		     meters tap post-trim/post-EQ/post-channel-fader per #3529 and track
		     each deck fader, not this master control). Distinct from the
		     output-health-bar below,
		     which answers "is a device receiving audio" rather than "how loud
		     is the master bus". -->
		<MasterLevelMeter active={masterMeterActive} />

		<!-- output-to-device bar: REAL -> audio-output-liveness verdict (pin
		     93c82bb36eb7). A 1px line under the master slider distinguishing "we
		     are sending audio" (the slider above) from "a device is actually
		     receiving it" (this line). Idle paints nothing rather than a false
		     "ok", per the pin's own "never healthy when the probe cannot tell"
		     rule. -->
		<div
			class={`output-health-bar ${outputHealthDisplay.cssClass}`}
			title={outputHealthDisplay.title}
			aria-label="output to audio device"
		></div>
		{#if audioOutputHealth.snapshot?.combined_verdict === 'not_delivering'}
			<div class="output-health-fault" title={outputHealthDisplay.title}>
				<span class="output-health-fault-copy"
					>Output device is not delivering audio. Switch the macOS output to another device and
					back, or reconnect the headphones.</span
				>
				<button
					type="button"
					class="output-health-switch-btn"
					disabled={switchOutputBusy || !outputHealthDisplay.switchOutputAvailable}
					title={outputHealthDisplay.switchOutputAvailable
						? 'Cycle the macOS default output away and back'
						: 'Switch output requires the installed macOS desktop shell'}
					onclick={() => void handleSwitchOutput()}
				>
					{switchOutputBusy ? 'Switching…' : 'Switch output'}
				</button>
			</div>
		{/if}
	</div>

	<!-- master mute: REAL -> gain 0 on the last node before the destination.
	     Opt-in at startup with ?muted=1 for headless UI-test agents. -->
	<button
		class="tb-icon mute-btn"
		class:muted={isMasterMuted()}
		aria-label="master mute"
		aria-pressed={isMasterMuted()}
		data-mute-reason={masterMuteReason() ?? undefined}
		title={isMasterMuted()
			? `${masterMuteReason() ?? ''} Master MUTED - final output gain forced to 0. The whole audio graph still runs, only the speaker feed is silent. Click to unmute (or ?muted=1 to start muted).`
			: 'Master audible. Click to mute the speaker feed - the audio graph keeps running, so nothing else changes.'}
		onclick={() => void runPerformanceCommandFromUi({ type: 'master_mute', muted: !isMasterMuted() })}
	>
		<!-- Muted says MUTED, because the button reports a STATE, not an
		     action; audible shows the speaker glyph, because there is no state
		     worth spelling out when nothing is wrong (pin 55a26655b749). -->
		{#if isMasterMuted() && masterMuteReason() !== null}
			MUTED (saved setting)
		{:else if isMasterMuted()}
			MUTED
		{:else}
			<svg width="14" height="12" viewBox="0 0 14 12" aria-hidden="true">
				<path d="M1 4.5h2.2L6 2v8L3.2 7.5H1z" fill="currentColor" />
				<path
					d="M8.4 4.1a3 3 0 0 1 0 3.8M10.3 2.6a5.4 5.4 0 0 1 0 6.8"
					fill="none"
					stroke="currentColor"
					stroke-width="1.2"
					stroke-linecap="round"
				/>
			</svg>
		{/if}
	</button>

	<!-- clock: REAL, local time HH:MM -->
	<span class="clock">{clock}</span>

	<!-- Account bauble. Not a rekordbox element, but sign-in has to be
	     reachable from performance mode too - the shell topbar is not
	     rendered on this route. Sized down to fit --rb-topbar-h, and labelled
	     because on this route it is the only sign-in affordance there is
	     (issue #2357). -->
	<CloudSyncStatusChip />
	<UserBauble size={20} showLabel />
</header>

{#if PairingSheet}
	<PairingSheet bind:open={pairingOpen} bind:snapshot={pairingSnapshot} />
{/if}

<!-- MIDI drawer: fixed overlay, only visible while midiUi.panelOpen -->
<MidiPanel />

<!-- Jobs drawer: overlay, only visible while jobsStore.drawerOpen -->
<JobsDrawer />

<!-- MIDI learn-log pop-out: click-through floating overlay, opened from the
     panel's "pop out" button. Only visible while midiUi.logPopoutOpen. -->
<MidiLearnLogPopout />

<style>
	.rb-topbar {
		position: relative;
		grid-area: topbar;
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 0 8px;
		height: var(--rb-topbar-h);
		color: var(--rb-text-dim);
		overflow: hidden;
	}


	.spacer-left {
		flex: 1;
	}

	.spacer {
		flex: 2;
	}

	.vibe-slot {
		position: static;
		flex: none;
		margin-inline: 2px;
	}
	.rb-topbar :global(.fb-cluster) {
		position: static;
		transform: none;
		flex: none;
		margin-inline: 2px;
	}
	/* NOTE (issue #2357): the `free-badge`/`topbar-slot-utility` tier that used
	   to sit here at 1400px, and the `clock` tier at 1320px, are now folded
	   into the 1530px tier further down this block. Their old thresholds were
	   measured against an unlabelled 20px bauble. */
	/* Pin T3 (packet 9h): between 1400px and 1180px nothing else yielded room,
	   so at 1280px the row's total natural content width exceeded the
	   viewport while every OTHER flex child still had its default
	   `min-width: auto` floor (it can shrink to its own min-content size and
	   no further). CommandEntry's `.cmd-entry { min-width: 0; }` (set so its
	   inline `.cmd-status` result text can ellipsis) is the one flex child
	   with NO such floor, so it silently absorbed the entire deficit and got
	   crushed to a few px - present in the DOM, invisible and unhittable,
	   with no console error. Measured Sun 6 Sep 2026 at 1280px: `.cmd-entry`
	   offsetWidth 5px against a natural width of ~132px, a ~127px deficit
	   nothing else could give back.

	   TRIED FIRST, MEASURED, REJECTED: pulling ONLY the 1180px tier's
	   non-hiding `.cmd-input` shrink (130px -> 86px, ~44px) forward, without
	   touching pairing/vibe. This does NOT clear the deficit - measured
	   page.evaluate offsetWidth with that rule alone active:
	     1280px: cmd-entry  5px (not hittable) <- the reported bug, UNFIXED
	     1300px: cmd-entry 25px (not hittable)
	     1340px: cmd-entry 65px (hittable, but see next paragraph)
	   Shrinking `.cmd-input`'s declared width does not stop `.cmd-entry`
	   from being crushed further, because `.cmd-entry`'s `min-width: 0`
	   removes ITS OWN floor as a flex item of `.rb-topbar` - the ~127px
	   deficit at 1280px is far larger than the ~44px the input shrink can
	   ever give back. Only pairing (47px) + vibe (150px) carry enough real
	   width to close a deficit that size.

	   TRIED SECOND, MEASURED, REJECTED: hiding pairing+vibe only up to
	   1300px (leaving them reappearing above it, as before) reopens the
	   SAME crush from 1301px onward while the deficit is still large:
	   1301px 26px, 1305px 30px, 1320px 45px (all not hittable) - and the
	   point where offsetWidth alone predicts "hittable" is NOT reliably
	   safe either: real elementFromPoint hit-tests measured 1310px
	   hittable but 1315-1330px NOT hittable despite a slightly LARGER
	   offsetWidth than 1310px, i.e. non-monotonic near the crush boundary.
	   Threading a breakpoint through that band is not a safe fix.

	   FIX: hide pairing+vibe up to 1340px - 25px of margin above the last
	   point measured inside the unstable band (1315-1330px) above, and
	   well short of the existing 1400px free-badge/utility breakpoint so
	   the eviction window stays as narrow as the geometry allows rather
	   than matching 1400px for convenience. Verified at 5px granularity
	   across the FULL [1280px, 1400px] band (26 points, plus 1366px, a
	   very common laptop width) with this rule active: `.cmd-entry`
	   offsetWidth is a flat 130px through 1340px, then rises smoothly and
	   monotonically from 68px (1345px) to 101px (1400px) once pairing+vibe
	   reappear - and every one of those 26 points is hittable via a real
	   elementFromPoint check, none borderline. 1401px shows a DIFFERENT,
	   pre-existing crush (free-badge/utility reappearing past the
	   untouched 1400px boundary) that reproduces identically with this
	   diff fully reverted - out of scope for this fix, flagged separately
	   (spawned task investigates it alongside the CI e2e-gate flake).

	   RE-MEASURED Wed 9 Sep 2026 (PARITY-02, issue #1002): the SOURCE toggle
	   added to this row is a 75px fixed-width control, and this budget had no
	   slack, so 1340px was no longer the right eviction point. Measured with
	   Playwright elementFromPoint at 5px granularity across [1340px, 1920px]
	   on one page, three configurations, same run:

	     A  SOURCE shown, this rule at 1340px (i.e. the state that reds):
	        .cmd-entry collapses from 130px at 1340px to 8px at 1345px, and the
	        command INPUT fails its own hit test from 1345px continuously to
	        1505px. It first passes at 1510px.
	     B  SOURCE shown, pairing+vibe evicted across the whole sweep:
	        .cmd-entry is a flat 130px and the input is hittable at EVERY one of
	        the 117 widths from 1340px to 1920px.
	     C  SOURCE hidden (the pre-#1002 baseline, this rule at 1340px):
	        hittable everywhere except 1410-1425px - the pre-existing >1400px
	        crush already described above, not caused by and not fixed by #1002.

	   So the deficit is real and this eviction window is what pays for it.
	   1530px = 1505px (the last width measured unstable in A) + the same 25px
	   margin the 1340px choice used. That is a DELIBERATE WIDENING of the
	   eviction window, 1340px -> 1530px: pairing and vibe now hide up to
	   1530px rather than 1340px. It was chosen over hiding SOURCE itself
	   because pairing and vibe are read-only status chrome while SOURCE and
	   the command entry are both controls, and this row's existing ranking
	   already evicts pairing+vibe first. Configuration B is what ships, and it
	   is strictly healthier than the C baseline: it also closes the 1410-1425
	   hole. */
	@media (max-width: 1180px) {
		.rb-topbar :global(.cmd-input) { width: 86px; }
		.rb-topbar :global(.cmd-status) { display: none; }
	}
	/* 1024px -> 1210px, re-measured Wed 9 Sep 2026 with SOURCE in the row
	   (same harness and method as the 1530px note above). Two findings, one
	   of them pre-existing:
	     - With SOURCE and this rule still at 1024px, the command input fails
	       its hit test continuously across [1089px, 1184px]. Attributable:
	       the same sweep with SOURCE hidden passes at every one of those
	       widths.
	     - [1029px, 1084px] fails in BOTH sweeps. That is the >1024px twin of
	       the >1400px crush noted above and predates #1002.
	   Evicting this cluster across [1024px, 1340px] clears every width in
	   both sweeps, so 1210px = 1184px (last width attributable to SOURCE) +
	   the same 25px margin, and it closes the pre-existing hole as a side
	   effect. This group is the right thing to drop first: every member is
	   placeholder chrome - the eight view icons and LINK are `rb-inert` and
	   `disabled`, PAD is a dim label - so nothing operable leaves the row. */
	@media (max-width: 1400px) {
		.rb-topbar .topbar-slot-stage { font-size: 0; }
		.rb-topbar .topbar-slot-stage::after { content: 'STG'; font-size: 9px; }
	}
	@media (max-width: 980px) {
		.rb-topbar .topbar-slot-bsm { font-size: 0; }
		.rb-topbar .topbar-slot-bsm::after { content: 'BSM'; font-size: 9px; }
		.rb-topbar .topbar-slot-autoplay > .bsm-toggle:first-child { font-size: 0; }
		.rb-topbar .topbar-slot-autoplay > .bsm-toggle:first-child::after { content: 'AP'; font-size: 9px; }
	}
	/* Issue #2357, measured Tue 15 Sep 2026 on the built SPA (playwright,
	   chromium, 800x600 fixture library, one page per configuration, overflow
	   read as `scrollWidth - clientWidth` on `.rb-topbar` plus a real
	   elementFromPoint hit test per child).

	   The account bauble arrived with a VISIBLE LABEL, "Sign in with Google",
	   because on this route it is the only sign-in affordance there is and a
	   bare circle reads as decoration. That label is ~100px of new permanent
	   row width - the row had NONE to give (its existing tiers were each
	   measured to a ~25px margin against a 20px unlabelled circle), so every
	   tier that RESTORES chrome now has to restore it ~100px later, and the
	   two tiers that never yielded at all give up their read-only status
	   surfaces. Swept at 5px granularity across [780px, 1920px]: before this
	   change the shipped ladder overflows at 160 of 229 widths, worst 184px,
	   in bands 780-1105 / 1215-1390 / 1405-1505 / 1535-1715; 	   after it, at 0 of
	   229. The bands above the old thresholds are exactly where the label did
	   not fit, which is why the thresholds - not the selectors - are what
	   moved.

	   RE-VERIFIED Thu 18 Sep 2026 (issue #1365): the 1530px and 1740px tiers
	   above already close the >1400px crush this issue filed - a fresh 5px
	   elementFromPoint sweep across [1400px, 1920px] (105 widths) reports zero
	   failures with the current ladder, so no further threshold move was needed
	   here; tests/e2e/performance-topbar-responsive.spec.ts now guards
	   PERF-UI-06 at the ladder boundaries.

	   What pays, in the order it yields. Read-only STATUS yields before any
	   control, which is the same ranking the 1530px note above states: the
	   live perf readout, the Gig/Prep posture chip and the CloudSync status
	   chip REPORT state and operate nothing, so the 1125px tier is where they
	   go. Below that the inert/duplicated chrome goes, then the clock, then
	   the free badge and the utility icons, then - only on a window too narrow
	   for the row to be honest about it - pairing and the vibe meter.

	   Deliberately NOT evicted, because each is the only door to something:
	   the compact lyric chip (the one surface that says the word lane is
	   hidden at this height, and tests/e2e/performance-topbar-responsive.spec.ts
	   asserts it visible at 800x600), Stage, JOBS, the master fader, mute and
	   the command entry above 1023px. */
	@media (max-width: 1530px) {
		/* Was 1400px (free badge + utility) and 1320px (clock), both measured
		   against the unlabelled circle. At 1440px - a very common window - the
		   label is worth 90px and these are worth 115px. */
		.rb-topbar .free-badge,
		.rb-topbar .topbar-slot-utility,
		.rb-topbar .clock { display: none; }
	}
	@media (max-width: 1415px) {
		/* Was 1210px. The inert view-icon cluster, LINK and PAD are 233px of
		   placeholder chrome that operate nothing; the deficits above 1211px run
		   to 138px, so 1415px is the last measured failing width plus the same
		   25px margin the other tiers use. Only the cluster's INERT members go:
		   the 2-deck toggle is a real control since V1 (PR #4923) and is about
		   22px, well inside the ~95px this tier frees at its tightest width. */
		.rb-topbar .icon-cluster > :global(.explainer:has(.rb-inert)),
		.rb-topbar .link-btn,
		.rb-topbar .topbar-slot-pad { display: none; }
	}
	@media (max-width: 1125px) {
		/* New tier. Status first (see the note above), and the chrome the 825px
		   tier used to evict, which now has to go 300px earlier because the label
		   is still in the row at those widths. */
		.rb-topbar :global(.perf-meters-root),
		.rb-topbar :global(.posture-chip),
		.rb-topbar :global(.cloudsync-status),
		.rb-topbar .topbar-slot-midi,
		.rb-topbar :global([data-testid="refresh-analysis"]) { display: none; }
	}
	@media (max-width: 1023px) {
		/* The command entry yields LAST of the narrow tier's set: it is the
		   agent-facing door to the same typed dispatcher every control here uses,
		   and tests/e2e/performance-topbar-responsive.spec.ts asserts it visible
		   at 1024px. Below 1024px it either fits or it is crushed to zero width,
		   and a crushed control is the worse of the two failure modes - it looks
		   operable and is not (same reasoning as the 825px note this replaces). */
		.rb-topbar :global(.cmd-entry) { display: none; }
	}
	/* >=825px and <=1023px, the above still leaves the sign-in pill as the
	   widest thing in the row. It is the deliverable of this issue, so it keeps
	   its label at every width; tests/e2e/performance-topbar-responsive.spec.ts
	   asserts it visible, labelled and hittable at 800x600. */

	@media (max-width: 1740px) {
		.rb-topbar .topbar-slot-vibe { display: none; }
		/* Create pairing is the ONLY door to the pairing capture sheet, not
		   read-only status, so it shrinks to a PAIR label (same idiom as STG,
		   BSM and AP) instead of leaving with the vibe meter. Hiding it here
		   hid it on every Mac laptop window (1470-1728px). Measured Fri 2 Oct
		   2026 (playwright, chromium, fixture library, 10px sweep 780-1780px
		   plus 2px across 1100-1260px): 34px wide, no row overflow at any
		   width, and the command input stays hittable everywhere except
		   1126-1136px, which the tier below covers. */
		.rb-topbar .topbar-slot-pairing { font-size: 0; }
		.rb-topbar .topbar-slot-pairing::after { content: 'PAIR'; font-size: 9px; }
	}
	@media (max-width: 1160px) {
		/* 1136px (the last width where PAIR crushed the command input) + the
		   same 25px margin the other tiers use. */
		.rb-topbar .topbar-slot-pairing { display: none; }
	}

	.ap-wrap {
		position: relative;
		display: inline-flex;
		align-items: center;
		border-radius: 3px;
	}
	.ap-wrap.on {
		border: 1px solid var(--rb-accent);
	}
	.ap-wrap.on > .bsm-toggle {
		border: none;
		box-shadow: none;
	}
	/* Pin fc60002b81a8 / PLAY-13: one enclosure with a single divider line. */
	.ap-wrap > .bsm-toggle:first-child {
		border-top-right-radius: 0;
		border-bottom-right-radius: 0;
		border-right: 1px solid color-mix(in srgb, var(--rb-accent) 55%, var(--rb-border));
	}
	.ap-wrap:not(.on) > .bsm-toggle:first-child {
		border-right: none;
	}
	.ap-next-btn {
		border-top-left-radius: 0;
		border-bottom-left-radius: 0;
		padding-left: 6px;
		padding-right: 6px;
	}
	.ap-wrap > .ap-next-btn:hover {
		color: var(--rb-accent);
	}
	/* The ">|" split is the least essential control in this row (an early-
	   trigger shortcut, not a required transport) - drop it first, at the
	   same 980px breakpoint this row already uses to abbreviate BSM/AutoPlay
	   labels, rather than let it compete for room with controls a DJ or an
	   agent actually needs. */
	@media (max-width: 980px) {
		.rb-topbar .topbar-slot-autoplay > .ap-next-btn { display: none; }
	}
	.ap-menu {
		position: fixed;
		z-index: 90;
		width: 220px;
		transform: translateX(-100%);
		padding: 8px 9px 9px;
		background: #0a0c0f;
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		box-shadow: 0 6px 18px rgba(0, 0, 0, 0.55);
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: 10px;
		line-height: 1.35;
		text-align: left;
	}
	.ap-head {
		margin: 0 0 6px;
		font-weight: 650;
		letter-spacing: 0.02em;
	}
	.ap-row {
		display: flex;
		align-items: center;
		gap: 6px;
		margin: 0 0 6px;
		cursor: pointer;
		color: var(--rb-text);
	}
	.ap-row input {
		margin: 0;
	}
	.ap-two-track {
		justify-content: space-between;
		width: 100%;
		padding: 0;
		border: 0;
		background: transparent;
		font: inherit;
		text-align: left;
	}
	.ap-planned {
		font-size: 8px;
		letter-spacing: 0.06em;
		text-transform: uppercase;
	}
	.ap-hint {
		margin: 0;
		color: var(--rb-text-dim);
	}

	.rb-topbar.vibe-rainbow {
		/* 200% size + identical end stop: shift by 50% = one seamless cycle.
		   --vr grows unbounded (no wrap) so there is no loop seam. */
		background: linear-gradient(
				90deg,
				#ff0040,
				#ff8000,
				#ffef00,
				#00e676,
				#00e5ff,
				#2979ff,
				#d500f9,
				#ff0040
			)
			calc(var(--vr) * 50%) 0 / 200% 100%;
		border-color: color-mix(in srgb, #c44dff 40%, var(--rb-border));
	}

	.mode-dd {
		display: flex;
		align-items: center;
		gap: 4px;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		letter-spacing: 0.05em;
		padding: 2px 8px;
		line-height: 1;
		cursor: pointer;
		list-style: none;
	}
	.mode-dd::-webkit-details-marker {
		display: none;
	}
	.mode-picker {
		position: relative;
		z-index: 20;
	}
	.mode-picker[open] > .mode-dd {
		border-color: var(--rb-accent);
		color: var(--rb-text);
	}
	.mode-menu {
		/* Fixed positioning escapes the topbar's deliberate overflow clip. */
		position: fixed;
		z-index: 100;
		top: calc(100% + 5px);
		left: 0;
		width: 300px;
		padding: 7px;
		background: #0a0c0f;
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		box-shadow: 0 6px 18px rgba(0, 0, 0, 0.55);
	}
	.mode-menu-heading {
		margin: 1px 3px 6px;
		color: var(--rb-text-dim);
		font-size: 9px;
		font-weight: 650;
		letter-spacing: 0.07em;
		text-transform: uppercase;
	}
	.mode-card {
		display: grid;
		grid-template-columns: 52px minmax(0, 1fr) auto;
		align-items: center;
		gap: 7px;
		width: 100%;
		min-height: 48px;
		padding: 5px;
		border: 1px solid transparent;
		border-radius: 2px;
		background: transparent;
		color: var(--rb-text);
		font: inherit;
		text-align: left;
		text-decoration: none;
	}
	a.mode-card:hover,
	a.mode-card:focus-visible {
		border-color: var(--rb-accent);
		background: color-mix(in srgb, var(--rb-accent) 12%, transparent);
		outline: none;
	}
	.mode-copy {
		display: grid;
		gap: 2px;
		min-width: 0;
		font-size: 9px;
		line-height: 1.25;
	}
	.mode-copy strong {
		font-size: 10px;
		letter-spacing: 0.03em;
	}
	.mode-gain {
		color: var(--rb-text);
	}
	.mode-lose {
		color: var(--rb-text-dim);
	}
	.mode-thumbnail {
		display: block;
		height: 36px;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		background-color: #141920;
	}
	.mode-thumbnail.gig {
		background:
			linear-gradient(90deg, transparent 48%, #72b9ff 48% 52%, transparent 52%),
			linear-gradient(#161d26 45%, #72b9ff 45% 52%, #161d26 52%);
	}
	.mode-thumbnail.prep {
		background:
			linear-gradient(180deg, #72b9ff 0 18%, transparent 18% 82%, #346c3e 82% 100%),
			repeating-linear-gradient(90deg, #1a222c 0 4px, #161d26 4px 8px);
	}
	.mode-thumbnail.library {
		background:
			linear-gradient(90deg, #3b79ad 0 22%, transparent 22% 28%, #346c3e 28% 50%, transparent 50% 56%, #7a5c33 56% 78%, transparent 78%),
			#161d26;
	}
	.mode-thumbnail.trackify {
		background:
			radial-gradient(circle at 50% 50%, #a8b2bf 0 12%, #303b48 13% 31%, #72b9ff 32% 36%, #161d26 37%),
			linear-gradient(90deg, transparent 42%, #72b9ff 42% 58%, transparent 58%);
	}

	.icon-cluster {
		display: flex;
		align-items: center;
		gap: 2px;
	}

	.tb-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		background: transparent;
		border: none;
		border-radius: 2px;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: 9px;
		padding: 2px 3px;
		line-height: 1;
	}
	.tb-icon.active {
		color: var(--rb-accent);
	}
	.tb-icon.theme-toggle {
		cursor: pointer;
	}
	.tb-icon.theme-toggle.on {
		color: var(--rb-accent);
	}
	.tb-icon.theme-toggle:hover {
		color: var(--rb-text);
	}
	.tb-icon.fx {
		border: 1px solid var(--rb-border);
		padding: 2px 4px;
	}
	.tb-icon.mute-btn {
		border: 1px solid var(--rb-border);
		cursor: pointer;
		letter-spacing: 0.06em;
		padding: 2px 4px;
	}
	.tb-icon.mute-btn:hover {
		color: var(--rb-text);
	}
	/* Muted is a loud state on purpose: a silent app must never look normal. */
	.tb-icon.mute-btn.muted {
		border-color: var(--rb-danger, #d24b4b);
		color: var(--rb-danger, #d24b4b);
	}

	.link-btn {
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		letter-spacing: 0.08em;
		padding: 2px 8px;
		line-height: 1;
	}

	.dim-label {
		font-size: var(--rb-fs-label);
		letter-spacing: 0.08em;
		color: var(--rb-text-dim);
		opacity: 0.6;
	}

	.bsm-toggle {
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: 9px;
		letter-spacing: 0.04em;
		padding: 2px 6px;
		line-height: 1.2;
		cursor: pointer;
	}
	.bsm-toggle.on {
		color: var(--rb-accent);
		border-color: color-mix(in srgb, var(--rb-accent) 55%, var(--rb-border));
		box-shadow: 0 0 0 1px color-mix(in srgb, var(--rb-accent) 25%, transparent);
	}
	.bsm-toggle:hover {
		color: var(--rb-text);
	}
	.bsm-toggle:disabled {
		cursor: not-allowed;
		opacity: 0.55;
	}
	.lyrics-compact-chip {
		display: none;
		align-items: center;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		background: color-mix(in srgb, var(--rb-orange) 18%, var(--rb-panel-raised));
		color: var(--rb-orange);
		font-family: var(--rb-font);
		font-size: 8px;
		letter-spacing: 0.04em;
		line-height: 1.2;
		padding: 2px 5px;
		white-space: nowrap;
	}
	@media (max-height: 799px) {
		.lyrics-compact-chip {
			display: inline-flex;
		}
	}
	/* MIDI label: LIVE status button. grey = unsupported/denied/idle,
	 * amber pulse = permission prompt pending, green = mapped device up. */
	.midi-label {
		background: transparent;
		border: none;
		padding: 0;
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		letter-spacing: 0.08em;
		line-height: 1;
		cursor: pointer;
	}
	.midi-label.st-grey {
		color: var(--rb-text-dim);
		opacity: 0.6;
	}
	.midi-label.st-grey:hover {
		opacity: 1;
	}
	.midi-label.st-green {
		color: var(--rb-green);
		opacity: 1;
	}
	.midi-label.st-red {
		color: var(--rb-red);
		opacity: 1;
	}
	.midi-glyph {
		margin-left: 3px;
		vertical-align: middle;
		flex: none;
	}
	.midi-label.st-amber {
		color: var(--rb-orange);
		opacity: 1;
		animation: midi-pulse 1s ease-in-out infinite;
	}
	@keyframes midi-pulse {
		0%,
		100% {
			opacity: 1;
		}
		50% {
			opacity: 0.35;
		}
	}

	.free-badge {
		background: var(--rb-yellow);
		color: #14171d;
		font-size: var(--rb-fs-label);
		font-weight: 600;
		border-radius: 2px;
		padding: 1px 6px;
		line-height: 1.4;
	}

	.master-slider-wrap {
		display: flex;
		flex-direction: column;
		flex: 0 0 auto;
		gap: 2px;
	}
	.master-slider {
		position: relative;
		width: 80px;
		height: 14px;
		cursor: ew-resize;
		touch-action: none;
		outline: none;
		flex: 0 0 auto;
	}
	.output-health-bar {
		width: 80px;
		height: 1px;
		flex: 0 0 auto;
		background: transparent;
	}
	.output-health-bar.ok {
		background: var(--rb-accent);
		opacity: 0.5;
	}
	.output-health-bar.dead {
		/* --rb-danger is never defined (see StemsPrompt.svelte); --rb-red is the
		   palette's real danger colour (theme.css). */
		background: var(--rb-red, #e55);
		opacity: 1;
		height: 2px;
		margin-top: -0.5px;
	}
	.output-health-bar.unknown {
		background: var(--rb-text-dim);
		opacity: 0.25;
	}
	.output-health-fault {
		display: flex;
		align-items: center;
		gap: 6px;
		max-width: 280px;
		font-size: 10px;
		line-height: 1.2;
		color: var(--rb-red, #e55);
	}
	.output-health-fault-copy {
		flex: 1 1 auto;
	}
	.output-health-switch-btn {
		flex: 0 0 auto;
		font-size: 10px;
		padding: 1px 6px;
		border: 1px solid currentColor;
		background: transparent;
		color: inherit;
		cursor: pointer;
	}
	.output-health-switch-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.master-track {
		position: absolute;
		left: 0;
		right: 0;
		top: 50%;
		height: 3px;
		margin-top: -1.5px;
		background: var(--rb-inset);
		border: 1px solid var(--rb-border);
	}
	.master-fill {
		position: absolute;
		left: 0;
		top: 50%;
		height: 3px;
		margin-top: -1.5px;
		background: var(--rb-accent);
	}
	.master-thumb {
		position: absolute;
		top: 3px;
		width: 8px;
		height: 8px;
		border-radius: 50%;
		background: var(--rb-chrome);
		border: 1px solid var(--rb-border);
	}
	.master-slider:focus-visible .master-thumb {
		box-shadow: 0 0 4px var(--rb-accent-glow);
	}

	.clock {
		font-size: 11px;
		color: var(--rb-text);
		font-variant-numeric: tabular-nums;
	}
</style>
