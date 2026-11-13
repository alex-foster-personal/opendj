<script lang="ts">
	/**
	 * The four triggers of one filled edge tab (spec 7.4, mock variant D `.in`): play/pause on
	 * the master, the muted repeat toggle, the region waveform that previews in the headphones,
	 * and the pencil. They render as direct children of EdgeTab's `.in` grid with the title and
	 * meta (`children`) between repeat and waveform, the mock's DOM order; CSS orders them per
	 * side so play sits at the screen edge. Every click passes the tab's guard (`guard`) and
	 * stops there, so it never also pins the card.
	 */
	import { onMount, type Snippet } from 'svelte';
	import { nextRepeat, repeatTip } from '$lib/player/sampler/sampler-repeat';
	import {
		fireSlot,
		openSlotEditor,
		slotPhase,
		togglePreview,
		updateSlot
	} from '$lib/player/sampler/sampler-store.svelte';
	import { regionBars, type Edge, type SamplerSlot } from '$lib/player/sampler/sampler-types';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { effectiveWavePalette, effectiveWaveScheme } from '$lib/rb/ui-skin';
	import { resolveWaveBandColors } from '$lib/rb/wave-palette';
	import { PENCIL_PATH, PLAY_TRIANGLE_PATH } from '$lib/ui/icon-glyphs';
	import { drawRegionPeaks } from './region-wave-draw';

	let {
		edge,
		slot,
		guard,
		children
	}: { edge: Edge; slot: SamplerSlot; guard: () => boolean; children: Snippet } = $props();

	const repeat = $derived(slot.settings.repeat);
	const repTip = $derived.by(() => {
		const bars = regionBars(slot);
		const next = repeatTip(nextRepeat(repeat), bars).split(':')[0].toLowerCase();
		return `${repeatTip(repeat, bars)}. Click for ${next}.`;
	});
	const phase = $derived(slotPhase(slot.slot_id));
	const busy = $derived(phase === 'armed' || phase === 'playing');
	const colors = $derived(
		resolveWaveBandColors(
			effectiveWaveScheme(uiPrefs.theme, uiPrefs.ui_skin),
			effectiveWavePalette(uiPrefs.wave_palette, uiPrefs.ui_skin)
		)
	);

	let canvas: HTMLCanvasElement | undefined = $state();
	function redraw(): void {
		if (canvas && canvas.clientWidth > 0) drawRegionPeaks(canvas, slot.peaks, colors);
	}
	// A tab hidden while the sampler is off has no size; redraw once it is shown again.
	onMount(() => {
		if (canvas === undefined) return;
		const ro = new ResizeObserver(redraw);
		ro.observe(canvas);
		return () => ro.disconnect();
	});
	$effect(redraw);

	function act(ev: MouseEvent, fn: () => unknown): void {
		ev.stopPropagation();
		if (guard()) void fn();
	}

	function cycleRepeat(): Promise<unknown> {
		const next = nextRepeat(repeat);
		const mode =
			next === 'once'
				? 'one_shot'
				: slot.settings.mode === 'one_shot'
					? 'loop'
					: slot.settings.mode;
		// updateSlot toasts and rolls back on failure; the catch stops a second, unhandled report.
		return updateSlot(slot.slot_id, { settings: { ...slot.settings, repeat: next, mode } }).catch(
			() => undefined
		);
	}
</script>

<button
	type="button"
	class="play"
	aria-label={busy ? 'Pause: stop this sample' : 'Play on the master at the next downbeat'}
	title={busy ? 'Pause' : 'Play at the next downbeat'}
	onclick={(ev) => act(ev, () => fireSlot(slot))}
	>{#if busy}&#10074;&#10074;{:else}<svg viewBox="0 0 16 16" aria-hidden="true"
			><path d={PLAY_TRIANGLE_PATH} fill="currentColor" /></svg
		>{/if}</button
>
<button
	type="button"
	class="rep"
	class:looping={repeat !== 'once'}
	title={repTip}
	aria-label={repTip}
	onclick={(ev) => act(ev, cycleRepeat)}
>
	{#if repeat === 'once'}[1]{:else}<span
			><svg viewBox="0 0 16 16" aria-hidden="true"
				><path d="M3 7a5 5 0 018.5-3.5L13 5M13 2v3h-3M13 9a5 5 0 01-8.5 3.5L3 11M3 14v-3h3" /></svg
			>{#if repeat !== 'loop'}<sup>{repeat}</sup>{/if}</span
		>{/if}
</button>
{@render children()}
<!-- svelte-ignore a11y_click_events_have_key_events -->
<div
	class="wv"
	role="button"
	tabindex="-1"
	title="Click: preview in the headphones only, beat and key matched"
	onclick={(ev) => act(ev, () => togglePreview(slot, phase === 'previewing'))}
>
	<canvas bind:this={canvas}></canvas><i class="ph"></i>
</div>
<button
	type="button"
	class="pen"
	aria-label="Sample settings"
	onclick={(ev) => act(ev, () => openSlotEditor(slot.slot_id, edge))}
	><svg viewBox="0 0 24 24" aria-hidden="true"
		><path d={PENCIL_PATH} fill="none" stroke="currentColor" stroke-width="2" /></svg
	></button
>

<style>
	:global(.edge-wave.R) .pen {
		order: 1;
	}
	:global(.edge-wave.R) .rep {
		order: 3;
	}
	:global(.edge-wave.R) .wv {
		order: 4;
	}
	:global(.edge-wave.R) .play {
		order: 5;
	}
	:global(.edge-wave.L) .play {
		order: 1;
	}
	:global(.edge-wave.L) .wv {
		order: 2;
	}
	:global(.edge-wave.L) .rep {
		order: 3;
	}
	:global(.edge-wave.L) .pen {
		order: 5;
	}
	.play {
		width: 28px;
		height: 28px;
		border-radius: 4px;
		border: 1px solid var(--es-border-strong);
		background: var(--es-play-bg);
		color: var(--es-fg);
		font-size: 12px;
		line-height: 1;
		cursor: pointer;
		padding: 0;
	}
	.play svg {
		width: 12px;
		height: 12px;
		vertical-align: middle;
	}
	.play:hover {
		border-color: var(--es-accent);
		color: var(--es-accent);
	}
	.rep {
		width: 22px;
		height: 20px;
		border-radius: 3px;
		border: 1px solid transparent;
		background: transparent;
		color: var(--es-dim);
		font: 500 10.5px var(--es-font-mono);
		line-height: 1;
		cursor: pointer;
		padding: 0;
		display: grid;
		place-items: center;
	}
	.rep svg {
		width: 13px;
		height: 13px;
		stroke: currentColor;
		fill: none;
		stroke-width: 1.7;
		stroke-linecap: round;
		stroke-linejoin: round;
	}
	.rep sup {
		font-size: 8px;
		margin-left: 1px;
	}
	.rep span {
		display: inline-flex;
		align-items: flex-start;
	}
	.rep.looping {
		color: var(--es-muted);
	}
	.rep:hover {
		border-color: var(--es-border-strong);
		color: var(--es-fg);
	}
	.pen {
		width: 24px;
		height: 24px;
		border-radius: 4px;
		background: transparent;
		border: 1px solid transparent;
		color: var(--es-muted);
		font-size: 14px;
		cursor: pointer;
		padding: 0;
		opacity: var(--treveal, 0);
	}
	.pen svg {
		width: 14px;
		height: 14px;
		vertical-align: middle;
	}
	.pen:hover {
		color: var(--es-fg);
		border-color: var(--es-border-strong);
	}
	.wv {
		position: relative;
		height: 34px;
		border-radius: 3px;
		background: transparent;
		cursor: crosshair;
		overflow: hidden;
	}
	.wv canvas {
		width: 100%;
		height: 100%;
		display: block;
	}
	.ph {
		position: absolute;
		top: 0;
		bottom: 0;
		left: 0;
		width: 2px;
		display: none;
	}
	:global(.wslot.playing) .ph {
		display: block;
		background: var(--es-playhead);
		box-shadow: 0 0 6px var(--es-accent);
	}
	:global(.wslot.previewing) .ph {
		display: block;
		background: var(--es-deck-a);
	}
	:global(.wslot.previewing) .wv {
		outline: 1px solid var(--es-deck-a);
	}
</style>
