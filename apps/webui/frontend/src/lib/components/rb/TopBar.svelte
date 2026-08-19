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
	import { engine, isMasterMuted, mixerState, setMasterMuted } from '$lib/rb/audio-engine.svelte';
	import type { AudioEngine } from '$lib/rb/types';
	import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';
	import {
		setAutoPlayEnabled,
		setAutoPlayEnforceOrder,
		setAutoPlayMaximizeReach,
		setBeatSyncMax,
		uiPrefs
	} from '$lib/rb/prefs.svelte';
	import { describeAutoPlayMode } from '$lib/rb/autoplay-mode';
	import { openSettings } from '$lib/settings/hotkeys';
	import { vibeState } from '$lib/rb/vibe.svelte';
	import { WHEEL_STEP, wheelAdjust } from '$lib/rb/wheel-adjust';
	import CommandEntry from './CommandEntry.svelte';
	import CreatePairingSheet from './CreatePairingSheet.svelte';
	import PerfMeters from './PerfMeters.svelte';
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

	interface MasterCapableEngine extends AudioEngine {
		setMaster(value: number): void;
	}

	const INERT_TITLE = 'not implemented - see PARITY-TODO';

	/** Why the JOBS toggle is inert, or null when the daemon offers jobs. A
	 * different category from INERT_TITLE above: the feature is built, this
	 * daemon simply does not serve it. */
	const jobsUnavailable = $derived(jobsRefusal());

	let pairingOpen = $state(false);
	let autoPlayMenuOpen = $state(false);
	let autoPlayWrapEl: HTMLSpanElement | undefined = $state();
	let autoPlayMenuStyle = $state('');

	const autoPlayTitle: string = $derived.by(() => {
		const d = describeAutoPlayMode(uiPrefs);
		if (d.mode === 'off') return `${d.short} - ${d.detail}`;
		return `AutoPlay ON (${d.short}) - last ~16s loads onto a free/stopped deck. ${d.detail}`;
	});

	function _placeAutoPlayMenu(): void {
		if (autoPlayWrapEl === undefined) return;
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
</script>

<header
	class="rb-topbar rb-panel"
	class:vibe-rainbow={vibeState.display >= 0.9}
	style={vibeState.display >= 0.9 ? `--vr:${vibeState.rainbow_index}` : undefined}
>	<!-- left: live audio health + prefetch count, then mode dropdown -->
	<PerfMeters />

	<!-- Stems separation, aggregate and live off jobs.updated. Renders nothing
	     while no stems job is active, so it costs no space the rest of the
	     time; clicking it opens the JOBS drawer for the per-job detail. -->
	<StemsProgress />

	<button class="mode-dd rb-inert" disabled title={INERT_TITLE}>
		PERFORMANCE
		<svg width="7" height="5" viewBox="0 0 7 5" aria-hidden="true">
			<path d="M0.5 1 L3.5 4 L6.5 1" fill="none" stroke="currentColor" stroke-width="1.2" />
		</svg>
	</button>

	<div class="icon-cluster">
		<!-- list-view icon with dropdown caret -->
		<button class="tb-icon rb-inert" disabled title={INERT_TITLE} aria-label="list view">
			<svg width="16" height="12" viewBox="0 0 16 12" aria-hidden="true">
				<rect x="1" y="1.5" width="9" height="1.6" fill="currentColor" />
				<rect x="1" y="5.2" width="9" height="1.6" fill="currentColor" />
				<rect x="1" y="8.9" width="9" height="1.6" fill="currentColor" />
				<path d="M11.5 5 L13.5 7 L15.5 5" fill="none" stroke="currentColor" stroke-width="1.1" />
			</svg>
		</button>
		<!-- FX panel toggle -->
		<button class="tb-icon fx rb-inert" disabled title={INERT_TITLE}>FX</button>
		<!-- split-view icon -->
		<button class="tb-icon rb-inert" disabled title={INERT_TITLE} aria-label="split view">
			<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
				<rect x="1" y="1" width="10" height="10" fill="none" stroke="currentColor" />
				<line x1="6" y1="1" x2="6" y2="11" stroke="currentColor" />
			</svg>
		</button>
		<!-- 2up icon -->
		<button class="tb-icon rb-inert" disabled title={INERT_TITLE} aria-label="2 deck view">
			<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
				<rect x="1" y="2" width="4.4" height="8" fill="none" stroke="currentColor" />
				<rect x="6.6" y="2" width="4.4" height="8" fill="none" stroke="currentColor" />
			</svg>
		</button>
		<!-- grid icon -->
		<button class="tb-icon rb-inert" disabled title={INERT_TITLE} aria-label="grid view">
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
		<button class="tb-icon active rb-inert" disabled title={INERT_TITLE} aria-label="4 waveform view">
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
		<button class="tb-icon rb-inert" disabled title={INERT_TITLE} aria-label="scope view 1">
			<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
				<circle cx="6" cy="6" r="4.6" fill="none" stroke="currentColor" />
				<circle cx="6" cy="6" r="1.4" fill="currentColor" />
			</svg>
		</button>
		<button class="tb-icon rb-inert" disabled title={INERT_TITLE} aria-label="scope view 2">
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

	<button class="link-btn rb-inert" disabled title={INERT_TITLE}>LINK</button>

	<div class="spacer"></div>

	<!-- dead-center vibe meter: mouse movement tops it up; history in localStorage -->
	<div class="vibe-slot">
		<VibeMeter />
	</div>

	<!-- right cluster -->
	<button
		type="button"
		class="bsm-toggle"
		title="Create pairing from two decks"
		onclick={() => (pairingOpen = true)}
	>
		Create pairing
	</button>

	<button
		type="button"
		class="bsm-toggle"
		class:on={uiPrefs.beat_sync_max}
		aria-pressed={uiPrefs.beat_sync_max}
		title={uiPrefs.beat_sync_max
			? 'BeatSyncMax ON - every seek (incl. master) keeps BAR phase lock'
			: 'BeatSyncMax OFF - followers sync on seek; master free-seeks'}
		onclick={() => setBeatSyncMax(!uiPrefs.beat_sync_max)}
	>
		BeatSyncMax
	</button>

	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<span
		class="ap-wrap"
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
				<p class="ap-hint">
					Off enforce: key +-1 + Beat Sync BPM. Maximize reach (default on) prefers
					fewer-outward twins so later tracks stay playable. Enforce order: next row
					after current (ignores maximize reach).
				</p>
			</div>
		{/if}
	</span>

	<span class="dim-label" title={INERT_TITLE}>PAD</span>
	<!-- MIDI: LIVE (build unit: midi panel) - status colour + panel toggle -->
	<button
		class="midi-label"
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
		class="midi-label"
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

	<button class="tb-icon rb-inert" disabled title={INERT_TITLE} aria-label="information">
		<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
			<circle cx="6" cy="6" r="5" fill="none" stroke="currentColor" />
			<rect x="5.3" y="5" width="1.4" height="4" fill="currentColor" />
			<rect x="5.3" y="2.6" width="1.4" height="1.4" fill="currentColor" />
		</svg>
	</button>

	<span class="free-badge">Free</span>

	<button
		type="button"
		class="tb-icon theme-toggle"
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

	<button class="tb-icon rb-inert" disabled title={INERT_TITLE} aria-label="refresh">
		<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
			<path d="M9.8 6 a3.8 3.8 0 1 1 -1.1 -2.7" fill="none" stroke="currentColor" stroke-width="1.2" />
			<path d="M9.9 0.8 L9.9 3.6 L7.1 3.6 Z" fill="currentColor" />
		</svg>
	</button>

	<!-- master volume: REAL -> engine master GainNode -->
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
		onclick={() => setMasterMuted(!isMasterMuted())}
	>
		{isMasterMuted() ? 'MUTE' : 'VOL'}
	</button>

	<!-- clock: REAL, local time HH:MM -->
	<span class="clock">{clock}</span>
</header>

<CreatePairingSheet bind:open={pairingOpen} />

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
		position: absolute;
		left: 50%;
		top: 50%;
		transform: translate(-50%, -50%);
		z-index: 1;
	}

	.ap-wrap {
		position: relative;
		display: inline-flex;
		align-items: center;
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

	.master-slider {
		position: relative;
		width: 80px;
		height: 14px;
		cursor: ew-resize;
		touch-action: none;
		outline: none;
		flex: 0 0 auto;
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
