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
	 * CONTRACT GAP (escalate): the AudioEngine interface in types.ts has
	 * no master-gain setter even though MixerState.master and
	 * COMPONENT-MAP 1.1 route this slider to the engine's master
	 * GainNode. This file assumes the engine implementation exposes
	 * `setMaster(value: number): void` beyond the interface; the cast
	 * below fails loudly at runtime if it does not (fail-fast, no
	 * silent fallback).
	 */
	import { engine } from '$lib/rb/audio-engine.svelte';
	import type { AudioEngine } from '$lib/rb/types';

	interface MasterCapableEngine extends AudioEngine {
		setMaster(value: number): void;
	}

	const INERT_TITLE = 'not implemented - see PARITY-TODO';

	/** 4-waveform view icon geometry: 4 rows of mini waveform bars (x,
	 * half-height) so the glyph reads as stacked waveforms, not a list. */
	const WAVE_ICON_ROWS_CY: number[] = [1.5, 4.3, 7.1, 9.9];
	const WAVE_ICON_BARS: { x: number; half: number }[] = [
		{ x: 1, half: 0.5 },
		{ x: 2.6, half: 1 },
		{ x: 4.2, half: 0.65 },
		{ x: 5.8, half: 1.15 },
		{ x: 7.4, half: 0.5 },
		{ x: 9, half: 0.9 },
		{ x: 10.6, half: 1.15 },
		{ x: 12.2, half: 0.65 }
	];

	let master = $state(1);
	let clock = $state(_formatClock(new Date()));
	let masterDragging = false;

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
		master = value;
		(engine as MasterCapableEngine).setMaster(value);
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
			_setMaster(_clamp01(master + 0.02));
		} else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') {
			e.preventDefault();
			_setMaster(_clamp01(master - 0.02));
		}
	}
</script>

<header class="rb-topbar rb-panel">
	<!-- left: mode dropdown + view-layout icon cluster -->
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
		     rows of varying-height mini waveform bars - must NOT read as a
		     plain list glyph (SCREENSHOT-SPEC 1). -->
		<button class="tb-icon active rb-inert" disabled title={INERT_TITLE} aria-label="4 waveform view">
			<svg width="14" height="12" viewBox="0 0 14 12" aria-hidden="true">
				{#each WAVE_ICON_ROWS_CY as cy (cy)}
					{#each WAVE_ICON_BARS as bar (bar.x)}
						<rect
							x={bar.x}
							y={cy - bar.half}
							width="0.9"
							height={2 * bar.half}
							fill="currentColor"
						/>
					{/each}
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

	<!-- center-left: LINK -->
	<button class="link-btn rb-inert" disabled title={INERT_TITLE}>LINK</button>

	<div class="spacer"></div>

	<!-- right cluster -->
	<span class="dim-label" title={INERT_TITLE}>PAD</span>
	<span class="dim-label" title={INERT_TITLE}>MIDI</span>

	<button class="tb-icon rb-inert" disabled title={INERT_TITLE} aria-label="information">
		<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
			<circle cx="6" cy="6" r="5" fill="none" stroke="currentColor" />
			<rect x="5.3" y="5" width="1.4" height="4" fill="currentColor" />
			<rect x="5.3" y="2.6" width="1.4" height="1.4" fill="currentColor" />
		</svg>
	</button>

	<span class="free-badge">Free</span>

	<button class="tb-icon rb-inert" disabled title={INERT_TITLE} aria-label="settings">
		<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
			<circle cx="6" cy="6" r="2" fill="none" stroke="currentColor" />
			<g stroke="currentColor" stroke-width="1.3">
				<line x1="6" y1="0.5" x2="6" y2="2.4" />
				<line x1="6" y1="9.6" x2="6" y2="11.5" />
				<line x1="0.5" y1="6" x2="2.4" y2="6" />
				<line x1="9.6" y1="6" x2="11.5" y2="6" />
				<line x1="2.1" y1="2.1" x2="3.5" y2="3.5" />
				<line x1="8.5" y1="8.5" x2="9.9" y2="9.9" />
				<line x1="2.1" y1="9.9" x2="3.5" y2="8.5" />
				<line x1="8.5" y1="3.5" x2="9.9" y2="2.1" />
			</g>
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
		aria-orientation="horizontal"
		aria-valuemin={0}
		aria-valuemax={1}
		aria-valuenow={master}
		tabindex="0"
		onpointerdown={handleMasterDown}
		onpointermove={handleMasterMove}
		onpointerup={handleMasterUp}
		onkeydown={handleMasterKeyDown}
	>
		<div class="master-track"></div>
		<div class="master-fill" style={`width: ${master * 100}%;`}></div>
		<div class="master-thumb" style={`left: calc(${master * 100}% - 4px);`}></div>
	</div>

	<!-- clock: REAL, local time HH:MM -->
	<span class="clock">{clock}</span>
</header>

<style>
	.rb-topbar {
		grid-area: topbar;
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 0 8px;
		height: var(--rb-topbar-h);
		color: var(--rb-text-dim);
		overflow: hidden;
	}

	.spacer {
		flex: 1;
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
	.tb-icon.fx {
		border: 1px solid var(--rb-border);
		padding: 2px 4px;
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
		background: #060809;
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
		background: #2a2f37;
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
