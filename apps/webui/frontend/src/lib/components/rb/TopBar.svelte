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
	import { onMount } from 'svelte';
	import { engine, isMasterMuted, mixerState } from '$lib/rb/audio-engine.svelte';
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
		setAutoPlayMaximizeReach,
		setBeatSyncMax,
		toggleTheme,
		uiPrefs
	} from '$lib/rb/prefs.svelte';
	import { describeAutoPlayMode } from '$lib/rb/autoplay-mode';
	import { openSettings } from '$lib/settings/hotkeys';
	import { vibeState } from '$lib/rb/vibe.svelte';
	import { WHEEL_STEP, wheelAdjust } from '$lib/rb/wheel-adjust';
	import { audioOutputHealth } from '$lib/rb/audio-output-health.svelte';
	import { describeAudioOutputHealth } from '$lib/rb/audio-output-health-display';
	import UserBauble from '$lib/components/UserBauble.svelte';
	import AnalysisSourceToggle from './AnalysisSourceToggle.svelte';
	import CloudSyncStatusChip from '$lib/components/CloudSyncStatusChip.svelte';
	import CommandEntry from './CommandEntry.svelte';
	import CreatePairingSheet from './CreatePairingSheet.svelte';
	import FeedbackWidget from './FeedbackWidget.svelte';
	import PerfMeters from './PerfMeters.svelte';
	import { plannedTitle } from '$lib/rb/planned-explainers';
	import StemsProgress from './StemsProgress.svelte';
	import VibeMeter from './VibeMeter.svelte';
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
	import { APP_MODES } from '$lib/rb/app-mode';

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
	let autoPlayMenuOpen = $state(false);
	let autoPlayWrapEl: HTMLSpanElement | undefined = $state();
	let autoPlayMenuStyle = $state('');
	let modePickerEl: HTMLDetailsElement | undefined = $state();
	let modeMenuStyle = $state('');

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

	function _placeAutoPlayMenu(): void {
		if (!autoPlayWrapEl) return;
		const r = autoPlayWrapEl.getBoundingClientRect();
		autoPlayMenuStyle = `left:${Math.round(r.right)}px;top:${Math.round(r.bottom + 6)}px`;
	}

	function _showAutoPlayMenu(): void {
		_placeAutoPlayMenu();
		autoPlayMenuOpen = true;
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

	function _placeModeMenu(): void {
		if (!modePickerEl?.open) return;
		const rect = modePickerEl.getBoundingClientRect();
		modeMenuStyle = `left:${Math.round(rect.left)}px;top:${Math.round(rect.bottom + 5)}px`;
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
	<StemsProgress />

	<details class="mode-picker" bind:this={modePickerEl} ontoggle={_placeModeMenu}>
		<summary class="mode-dd" aria-label="Choose app mode" title="App mode picker - PERFORMANCE is the current mode">
			PERFORMANCE
			<svg width="7" height="5" viewBox="0 0 7 5" aria-hidden="true">
				<path d="M0.5 1 L3.5 4 L6.5 1" fill="none" stroke="currentColor" stroke-width="1.2" />
			</svg>
		</summary>
		<div class="mode-menu" style={modeMenuStyle} aria-label="App modes">
			<p class="mode-menu-heading">Choose app mode</p>
			{#each APP_MODES as mode (mode.id)}
				{#if mode.available}
					<a class="mode-card" href={mode.href} aria-current={mode.id === 'performance' ? 'page' : undefined}>
						<span class={`mode-thumbnail ${mode.thumbnail}`} aria-hidden="true"></span>
						<span class="mode-copy">
							<strong>{mode.label}</strong>
							<span>{mode.description}</span>
						</span>
					</a>
				{:else}
					<button class="mode-card" type="button" disabled={!mode.available} title={mode.unavailableReason}>
						<span class={`mode-thumbnail ${mode.thumbnail}`} aria-hidden="true"></span>
						<span class="mode-copy">
							<strong>{mode.label}</strong>
							<span>{mode.description}</span>
						</span>
						<span class="mode-unavailable">Not available</span>
					</button>
				{/if}
			{/each}
		</div>
	</details>

	<div class="icon-cluster">
		<!-- list-view icon with dropdown caret -->
		<button class="tb-icon rb-inert" disabled title={plannedTitle('list-view')} aria-label="list view">
			<svg width="16" height="12" viewBox="0 0 16 12" aria-hidden="true">
				<rect x="1" y="1.5" width="9" height="1.6" fill="currentColor" />
				<rect x="1" y="5.2" width="9" height="1.6" fill="currentColor" />
				<rect x="1" y="8.9" width="9" height="1.6" fill="currentColor" />
				<path d="M11.5 5 L13.5 7 L15.5 5" fill="none" stroke="currentColor" stroke-width="1.1" />
			</svg>
		</button>
		<!-- FX panel toggle -->
		<button class="tb-icon fx rb-inert" disabled title={plannedTitle('fx')}>FX</button>
		<!-- split-view icon -->
		<button class="tb-icon rb-inert" disabled title={plannedTitle('split-view')} aria-label="split view">
			<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
				<rect x="1" y="1" width="10" height="10" fill="none" stroke="currentColor" />
				<line x1="6" y1="1" x2="6" y2="11" stroke="currentColor" />
			</svg>
		</button>
		<!-- 2up icon -->
		<button class="tb-icon rb-inert" disabled title={plannedTitle('2-deck-view')} aria-label="2 deck view">
			<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
				<rect x="1" y="2" width="4.4" height="8" fill="none" stroke="currentColor" />
				<rect x="6.6" y="2" width="4.4" height="8" fill="none" stroke="currentColor" />
			</svg>
		</button>
		<!-- grid icon -->
		<button class="tb-icon rb-inert" disabled title={plannedTitle('grid-view')} aria-label="grid view">
			<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
				<rect x="1" y="1" width="4.4" height="4.4" fill="none" stroke="currentColor" />
				<rect x="6.6" y="1" width="4.4" height="4.4" fill="none" stroke="currentColor" />
				<rect x="1" y="6.6" width="4.4" height="4.4" fill="none" stroke="currentColor" />
				<rect x="6.6" y="6.6" width="4.4" height="4.4" fill="none" stroke="currentColor" />
			</svg>
		</button>
		<!-- 4-waveform icon: the ACTIVE layout, painted blue statically. Four
		     stacked jagged polylines - must NOT read as a plain list glyph or
		     a dotted grid (SCREENSHOT-SPEC 1). -->
		<button class="tb-icon active rb-inert" disabled title={plannedTitle('4-waveform-view')} aria-label="4 waveform view">
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
		<!-- 2 circular scope icons -->
		<button class="tb-icon rb-inert" disabled title={plannedTitle('scope-view-1')} aria-label="scope view 1">
			<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
				<circle cx="6" cy="6" r="4.6" fill="none" stroke="currentColor" />
				<circle cx="6" cy="6" r="1.4" fill="currentColor" />
			</svg>
		</button>
		<button class="tb-icon rb-inert" disabled title={plannedTitle('scope-view-2')} aria-label="scope view 2">
			<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
				<circle cx="6" cy="6" r="4.6" fill="none" stroke="currentColor" />
				<path d="M6 1.4 A4.6 4.6 0 0 1 10.6 6" fill="none" stroke="currentColor" stroke-width="1.6" />
			</svg>
		</button>
	</div>

	<!-- center-left: LINK, given clear room from the left icon cluster so it
	     reads as centre-left rather than butted against the left group
	     (SCREENSHOT-SPEC 1). -->
	<div class="spacer-left"></div>

	<button class="link-btn rb-inert" disabled title={plannedTitle('link')}>LINK</button>

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

	<!-- right cluster -->
	<button
		type="button"
		class="bsm-toggle topbar-slot-pairing"
		title="Create pairing from two decks"
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

	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<span
		class="ap-wrap topbar-slot-autoplay"
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
			class:on={autoPlayNextState.armed}
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
		MIDI{#if midiGlyph !== ''}<span class="midi-glyph" aria-hidden="true">{midiGlyph}</span>{/if}
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
		     meters shipped in PR #1062 tap post-EQ/pre-fader and so do not move
		     with this control). Distinct from the output-health-bar below,
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
			class={`output-health-bar ${describeAudioOutputHealth(audioOutputHealth.snapshot).cssClass}`}
			title={describeAudioOutputHealth(audioOutputHealth.snapshot).title}
			aria-label="output to audio device"
		></div>
	</div>

	<!-- master mute: REAL -> gain 0 on the last node before the destination.
	     Opt-in at startup with ?muted=1 for headless UI-test agents. -->
	<button
		class="tb-icon mute-btn"
		class:muted={isMasterMuted()}
		aria-label="master mute"
		aria-pressed={isMasterMuted()}
		title={isMasterMuted()
			? 'Master MUTED - final output gain forced to 0. The whole audio graph still runs, only the speaker feed is silent. Click to unmute (or ?muted=1 to start muted).'
			: 'Master audible. Click to mute the speaker feed - the audio graph keeps running, so nothing else changes.'}
		onclick={() => void runPerformanceCommandFromUi({ type: 'master_mute', muted: !isMasterMuted() })}
	>
		<!-- Muted says MUTED, because the button reports a STATE, not an
		     action; audible shows the speaker glyph, because there is no state
		     worth spelling out when nothing is wrong (pin 55a26655b749). -->
		{#if isMasterMuted()}
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
	     rendered on this route. Sized down to fit --rb-topbar-h. -->
	<CloudSyncStatusChip />
	<UserBauble size={20} />
</header>

<CreatePairingSheet bind:open={pairingOpen} bind:snapshot={pairingSnapshot} />

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
	@media (max-width: 1400px) {
		.rb-topbar .free-badge,
		.rb-topbar .topbar-slot-utility { display: none; }
	}
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
	@media (max-width: 1530px) {
		.rb-topbar .topbar-slot-pairing,
		.rb-topbar .topbar-slot-vibe { display: none; }
	}
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
	@media (max-width: 1210px) {
		.rb-topbar .icon-cluster,
		.rb-topbar .link-btn,
		.rb-topbar .topbar-slot-pad { display: none; }
	}
	@media (max-width: 980px) {
		.rb-topbar .topbar-slot-bsm { font-size: 0; }
		.rb-topbar .topbar-slot-bsm::after { content: 'BSM'; font-size: 9px; }
		.rb-topbar .topbar-slot-autoplay > .bsm-toggle:first-child { font-size: 0; }
		.rb-topbar .topbar-slot-autoplay > .bsm-toggle:first-child::after { content: 'AP'; font-size: 9px; }
	}
	/* 820px -> 825px, re-measured Wed 9 Sep 2026 with SOURCE in the row (same
	   harness and method as the two notes above, but swept at 1px rather than
	   5px granularity because the band in question turned out to be 5px wide).
	   This rule EVICTS the command entry outright, so every width it covers
	   reads as "input not hittable" by design, and the sweep numbers have to
	   be read against that:
	     - pre-#1002 baseline (SOURCE hidden): the input is hittable at every
	       width from 821px to 1920px, and not below, i.e. the eviction
	       boundary and the crush floor coincide exactly at 820/821.
	     - with SOURCE and this rule still at 820px: the input is CRUSHED
	       (present, laid out, not hittable) across [821px, 825px], then
	       hittable at every width from 826px to 1920px.
	   So SOURCE costs this row exactly 5px of floor. Moving the boundary to
	   825px is a DELIBERATE 5px WIDENING of an eviction window that already
	   drops the command entry: it converts [821px, 825px] from a visible but
	   unclickable control into the same honest "not shown at this width"
	   state the four narrower pixels already had. A crushed control is the
	   worse of the two failure modes, since it looks operable and is not.
	   Below the 1024px this row is designed for either way. */
	@media (max-width: 825px) {
		.rb-topbar .topbar-slot-midi,
		.rb-topbar .topbar-slot-utility,
		.rb-topbar .free-badge,
		.rb-topbar .clock,
		.rb-topbar :global(.cmd-entry),
		.rb-topbar :global([data-testid="refresh-analysis"]),
		.rb-topbar :global(.bauble-root) { display: none; }
	}

	.ap-wrap {
		position: relative;
		display: inline-flex;
		align-items: center;
	}
	/* Pin fc60002b81a8: the ">|" next-track trigger reads as one button with
	   the AutoPlay toggle plus an RHS section, not two separate controls. */
	.ap-wrap > .bsm-toggle:first-child {
		border-top-right-radius: 0;
		border-bottom-right-radius: 0;
		border-right: none;
	}
	.ap-next-btn {
		border-top-left-radius: 0;
		border-bottom-left-radius: 0;
		padding-left: 6px;
		padding-right: 6px;
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
	button.mode-card:disabled {
		cursor: not-allowed;
		opacity: 0.52;
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
	.mode-copy > span {
		color: var(--rb-text-dim);
	}
	.mode-unavailable {
		color: var(--rb-text-dim);
		font-size: 8px;
		text-transform: uppercase;
	}
	.mode-thumbnail {
		display: block;
		height: 36px;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		background-color: #141920;
	}
	.mode-thumbnail.decks {
		background:
			linear-gradient(90deg, transparent 48%, #72b9ff 48% 52%, transparent 52%),
			linear-gradient(#161d26 45%, #72b9ff 45% 52%, #161d26 52%);
	}
	.mode-thumbnail.library {
		background:
			linear-gradient(90deg, #3b79ad 0 22%, transparent 22% 28%, #346c3e 28% 50%, transparent 50% 56%, #7a5c33 56% 78%, transparent 78%),
			#161d26;
	}
	.mode-thumbnail.player {
		background:
			radial-gradient(circle at 50% 50%, #a8b2bf 0 12%, #303b48 13% 31%, #72b9ff 32% 36%, #161d26 37%);
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
		font-size: 10px;
		font-weight: 700;
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
