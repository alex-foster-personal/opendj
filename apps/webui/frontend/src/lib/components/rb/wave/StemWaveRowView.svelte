<script lang="ts">
	// Props-only half of StemWaveRow: one stem's mini-waveform strip, or its
	// dimmed placeholder with the reason on hover. StemWaveRow.svelte owns the
	// envelope fetch and paints the canvas it gets back through `canvasEl`.
	import type { StemControl } from '$lib/rb/stem-types';

	let {
		stem,
		visible,
		unavailableTip,
		canvasEl = $bindable(null)
	}: {
		stem: StemControl;
		/** Stems shown AND a track loaded; false renders nothing. */
		visible: boolean;
		/** Why this stem has no strip, or null when it can paint. */
		unavailableTip: string | null;
		canvasEl?: HTMLCanvasElement | null;
	} = $props();
</script>

{#if visible}
	<div
		class="stem-wave-row"
		class:inert={unavailableTip !== null}
		data-stem={stem}
		data-unavailable={unavailableTip !== null}
		aria-disabled={unavailableTip !== null}
		title={unavailableTip ?? `${stem} stem mini-waveform`}
	>
		{#if unavailableTip !== null}
			<span class="stem-wave-placeholder">{stem.toUpperCase()}</span>
		{:else}
			<canvas bind:this={canvasEl} aria-hidden="true"></canvas>
		{/if}
	</div>
{/if}

<style>
	.stem-wave-row {
		height: var(--rb-stemwave-h, 12px);
		position: relative;
		background: color-mix(in srgb, var(--rb-bg) 92%, transparent);
		border-top: 1px solid color-mix(in srgb, var(--rb-border) 60%, transparent);
	}
	.stem-wave-row.inert {
		opacity: 0.45;
		pointer-events: none;
	}
	.stem-wave-placeholder {
		display: block;
		padding: 0 6px;
		font-size: 8px;
		line-height: var(--rb-stemwave-h, 12px);
		color: var(--rb-text-dim);
		letter-spacing: 0.08em;
	}
	canvas {
		display: block;
		width: 100%;
		height: var(--rb-stemwave-h, 12px);
	}
</style>
