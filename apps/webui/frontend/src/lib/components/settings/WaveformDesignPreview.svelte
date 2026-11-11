<script lang="ts">
	import type { WaveformDesign } from '$lib/rb/waveform-design';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { resolveWaveBandColors } from '$lib/rb/wave-palette';
	import { effectiveWaveformDesign, effectiveWavePalette } from '$lib/rb/ui-skin';
	import type { AnlzWaveform } from '$lib/rb/anlz-types';
	import WaveformDesignPreviewView from './WaveformDesignPreviewView.svelte';
	import { blockSnapLoadError } from '$lib/components/rb/wave/block-layout-lazy.svelte';
	import { paintWaveformPreview } from '$lib/settings/waveform-preview-paint';

	let { design, label = 'Waveform design preview' }: { design?: WaveformDesign; label?: string } = $props();

	// The EFFECTIVE look, not the stored token: an 'auto' pref resolves through
	// the active skin, so switching to Gothic repaints this preview as blocks in
	// mono grayscale (Mon 5 Oct 2026).
	const active = $derived(design ?? effectiveWaveformDesign(uiPrefs.waveform_design, uiPrefs.ui_skin));
	const palette = $derived(effectiveWavePalette(uiPrefs.wave_palette, uiPrefs.ui_skin));
	// The preview canvas is always painted on a dark fill, so it shows the
	// dark-face hues of the effective band palette (issue #4219): the deck
	// row's own colors (the theme.css mirror), since it paints through the
	// deck row's shading code (W15).
	const colors = $derived(resolveWaveBandColors('dark', palette));
	const waveHeight = $derived(uiPrefs.wave_height);

	/** The real-track sample, a lazy chunk: only the waveform DATA loads late
	 * (the component itself stays a static import, CHROME-16). A failed load
	 * throws rather than leaving a silently empty preview. */
	let sample: AnlzWaveform | null = $state(null);

	$effect(() => {
		let live = true;
		void import('$lib/settings/waveform-preview-sample').then((m) => {
			if (live) sample = m.previewSampleWaveform();
		});
		return () => {
			live = false;
		};
	});
</script>

<WaveformDesignPreviewView
	paint={paintWaveformPreview}
	{active}
	{palette}
	{colors}
	{sample}
	{waveHeight}
	gridLines={uiPrefs.wave_grid_lines}
	{label}
/>
{#if active === 'blocks' && blockSnapLoadError() !== null}
	<!-- DECKUX-42: say why deck rows paint the fixed pitch rather than look snapped-but-wrong.
	     Reload, not a promise to retry: the browser can keep a failed module
	     fetch for the life of the page (CHROME-07), so only a fresh page is
	     sure to recover. Nothing reloads on its own. -->
	<p class="wfd-err" data-testid="block-snap-error" title={`Block edge snapping failed to load: ${blockSnapLoadError()}`}>
		Edge snapping failed to load.
		<button type="button" class="wfd-reload" data-testid="block-snap-reload" onclick={() => location.reload()}>Reload</button>
	</p>
{/if}

<style>
	.wfd-err {
		margin: 4px 0 0;
		font-size: 11px;
		color: var(--rb-text);
		text-decoration: underline wavy;
	}
	.wfd-reload {
		margin-left: 4px;
		font-size: 11px;
	}
</style>
