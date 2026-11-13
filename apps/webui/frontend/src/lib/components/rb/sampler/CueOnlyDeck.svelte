<script lang="ts">
	/**
	 * CUE-only deck inside the edit modal (spec 7.4, mock `.cuedeck`): a CUE play/pause, a
	 * large copy of the tab's 3-band wave with the region's beatgrid (downbeats brighter) and
	 * a playhead moved by transform only. It is the tab waveform's preview (`togglePreview`,
	 * cue bus only, one preview at a time). Until an audio engine registers it says it is not
	 * ready and CUE is disabled: nothing pretends to play.
	 */
	import { onMount } from 'svelte';
	import { sampler, slotPhase, togglePreview } from '$lib/player/sampler/sampler-store.svelte';
	import type { SamplerSlot } from '$lib/player/sampler/sampler-types';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { effectiveWavePalette, effectiveWaveScheme } from '$lib/rb/ui-skin';
	import { resolveWaveBandColors } from '$lib/rb/wave-palette';
	import { PLAY_TRIANGLE_PATH } from '$lib/ui/icon-glyphs';
	import { drawRegionBeats, drawRegionPeaks } from './region-wave-draw';

	let { slot, info }: { slot: SamplerSlot; info: string } = $props();

	const CUE_TIP =
		'CUE-only deck: previews this sample in the headphones, beat and key matched to the MASTER deck. Nothing reaches the master output.';
	const NOT_READY = "Not ready: the sampler's audio engine has not loaded, so CUE cannot play yet.";

	const ready = $derived(sampler.engineReady);
	const previewing = $derived(slotPhase(slot.slot_id) === 'previewing');
	const colors = $derived(
		resolveWaveBandColors(
			effectiveWaveScheme(uiPrefs.theme, uiPrefs.ui_skin),
			effectiveWavePalette(uiPrefs.wave_palette, uiPrefs.ui_skin)
		)
	);

	let canvas: HTMLCanvasElement | undefined = $state();
	let ph: HTMLElement | undefined = $state();

	function redraw(): void {
		if (!canvas || canvas.clientWidth === 0) return;
		drawRegionPeaks(canvas, slot.peaks, colors);
		drawRegionBeats(canvas, slot.beats, slot.settings.downbeat_offset);
	}
	onMount(() => {
		if (canvas === undefined) return;
		const ro = new ResizeObserver(redraw);
		ro.observe(canvas);
		return () => ro.disconnect();
	});
	$effect(redraw);

	/** Move the playhead to `fraction` (0..1) of the region; the engine's frame loop calls it. */
	export function paint(fraction: number): void {
		if (!ph || !canvas) return;
		const f = Math.min(1, Math.max(0, fraction));
		ph.style.transform = `translateX(${(f * (canvas.clientWidth - 2)).toFixed(1)}px)`;
	}
</script>

<div class="cuedeck">
	<div class="chead">
		<button
			type="button"
			class="cuebtn"
			aria-pressed={previewing}
			disabled={!ready}
			data-tip={ready ? CUE_TIP : NOT_READY}
			title={ready ? CUE_TIP : NOT_READY}
			onclick={() => void togglePreview(slot, previewing)}
			>{#if previewing}&#10074;&#10074; CUE{:else}<svg
					viewBox="0 0 16 16"
					width="10"
					height="10"
					aria-hidden="true"><path d={PLAY_TRIANGLE_PATH} fill="currentColor" /></svg
				> CUE{/if}</button
		>
		{#if ready}
			<span class="cinfo">{info}</span>
		{:else}
			<span class="cinfo notready" role="status" data-testid="cue-deck-not-ready">{NOT_READY}</span>
		{/if}
	</div>
	<div class="cwrap">
		<canvas bind:this={canvas}></canvas><i class="cph" class:on={previewing} bind:this={ph}></i>
	</div>
</div>

<style>
	.cuedeck {
		border: 1px solid var(--es-cuedeck-border);
		border-radius: 5px;
		background: var(--es-cuedeck-bg);
		padding: 8px;
		display: grid;
		gap: 6px;
	}
	.chead {
		display: flex;
		align-items: center;
		gap: 8px;
		font: 12px var(--es-font-mono);
		color: var(--es-muted);
	}
	.cuebtn {
		font: 14px var(--es-font-display);
		letter-spacing: 0.6px;
		border: 1px solid var(--es-deck-a);
		color: var(--es-deck-a);
		background: transparent;
		border-radius: 4px;
		padding: 2px 10px;
		cursor: pointer;
		min-width: 92px;
	}
	.cuebtn[aria-pressed='true'] {
		background: var(--es-deck-a);
		color: var(--es-cue-ink);
	}
	.cuebtn:disabled {
		border-color: var(--es-border-strong);
		color: var(--es-dim);
		cursor: not-allowed;
	}
	.notready {
		color: var(--es-warn);
	}
	.cwrap {
		position: relative;
		height: 84px;
	}
	canvas {
		width: 100%;
		height: 100%;
		display: block;
	}
	.cph {
		position: absolute;
		top: 0;
		bottom: 0;
		left: 0;
		width: 2px;
		background: var(--es-deck-a);
		display: none;
		will-change: transform;
	}
	.cph.on {
		display: block;
	}
</style>
