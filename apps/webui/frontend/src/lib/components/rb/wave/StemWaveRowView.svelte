<script lang="ts">
	// Props-only half of StemWaveRow: one stem's mini-waveform strip, or its
	// dimmed placeholder with the reason on hover. StemWaveRow.svelte owns the
	// envelope fetch and paints the canvas it gets back through `canvasEl`.
	// A live row is a pointer target (issue #10317): click solos the stem,
	// Shift+click mutes it; the event never reaches the main waveform's seek.
	// An inert row stays inert and sends nothing.
	import type { StemControl, StemControlState } from '$lib/rb/stem-types';
	import type { StemRowAudibility } from './stem-waveform-ui';

	let {
		stem,
		visible,
		unavailableTip,
		audibility,
		canvasEl = $bindable(null),
		deckId = null,
		controlState = null,
		onSolo,
		onMute
	}: {
		stem: StemControl;
		/** Stems shown AND a track loaded; false renders nothing. */
		visible: boolean;
		/** Why this stem has no strip, or null when it can paint. */
		unavailableTip: string | null;
		/** What the listener hears from this stem, null when stems are not ready. */
		audibility: StemRowAudibility | null;
		canvasEl?: HTMLCanvasElement | null;
		/** Deck number for the testid. */
		deckId?: number | null;
		controlState?: Pick<StemControlState, 'muted' | 'solo'> | null;
		onSolo?: (pressT0Ms: number) => void;
		onMute?: (pressT0Ms: number) => void;
	} = $props();

	const inert = $derived(unavailableTip !== null);

	function onPointerDown(event: PointerEvent): void {
		if (inert) return;
		event.stopPropagation();
		event.preventDefault();
	}

	function onClick(event: MouseEvent): void {
		if (inert) return;
		event.stopPropagation();
		const t0 = typeof event.timeStamp === 'number' ? event.timeStamp : performance.now();
		if (event.shiftKey) onMute?.(t0);
		else onSolo?.(t0);
	}
</script>

{#if visible}
	<!-- Pointer-only control: the keyboard path is the deck stem chips
	     (data-hotkey-pointer-only, tabindex -1, like the seek canvas). -->
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<div
		class="stem-wave-row"
		class:inert
		role="button"
		tabindex="-1"
		data-hotkey-pointer-only
		data-testid={deckId === null ? undefined : `stem-wave-row-${stem}-deck-${deckId}`}
		aria-label={inert
			? `${stem} stem unavailable`
			: `${stem} stem: click to solo, Shift+click to mute`}
		aria-pressed={inert ? false : (controlState?.solo ?? false)}
		onpointerdown={onPointerDown}
		onclick={onClick}
		data-stem={stem}
		data-audibility={audibility ?? undefined}
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
		cursor: pointer;
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
