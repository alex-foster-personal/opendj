<script lang="ts">
	// Real precomputed Demucs controls. Click toggles mute; Shift+click toggles
	// solo. Both paths dispatch through the typed browser-agent command surface.
	import type { DeckState, StemControl } from '$lib/rb/types';

	let {
		deck,
		pending,
		onMute,
		onSolo
	}: {
		deck: DeckState;
		pending: boolean;
		onMute: (stem: StemControl) => Promise<void>;
		onSolo: (stem: StemControl) => Promise<void>;
	} = $props();

	const STEMS: readonly { id: StemControl; label: string; colorVar: string }[] = [
		{ id: 'vocal', label: 'VOCAL', colorVar: 'var(--rb-green)' },
		{ id: 'instrumental', label: 'INST', colorVar: 'var(--rb-orange)' },
		{ id: 'drums', label: 'DRUMS', colorVar: 'var(--rb-accent)' }
	];
	const ready: boolean = $derived(deck.stems.status === 'ready');

	/** A control this bundle's layout cannot drive. RoFormer's 2-stem split
	 * folds the drums into `instrumental`, so DRUMS has no signal of its own:
	 * the chip renders inert with a reason rather than accepting a click that
	 * could never change what you hear. */
	function unavailable(stem: StemControl): boolean {
		return ready && !deck.stems.available_controls.includes(stem);
	}

	const statusTip: string = $derived(
		ready
			? `real ${deck.stems.model ?? 'Demucs'} stems (${deck.stems.layout ?? 'demucs4'})` +
				` - click mute, Shift+click solo`
			: `stems ${deck.stems.status}: ${deck.stems.error ?? 'no aligned artifact'}`
	);

	function chipTip(stem: StemControl, label: string): string {
		if (unavailable(stem)) {
			return (
				`${label} is not a separate stem in this ${deck.stems.layout ?? 'bundle'} ` +
				`- it is mixed into INST, so it cannot be muted on its own`
			);
		}
		return statusTip;
	}

	async function toggle(event: MouseEvent, stem: StemControl): Promise<void> {
		if (event.shiftKey) await onSolo(stem);
		else await onMute(stem);
	}
</script>

<div class="stems" data-stems-status={deck.stems.status} title={statusTip}>
	<span class="mute">MUTE</span>
	{#each STEMS as stem (stem.id)}
		<button
			class="chip"
			class:muted={deck.stems.controls[stem.id].muted}
			class:solo={deck.stems.controls[stem.id].solo}
			class:unavailable={unavailable(stem.id)}
			disabled={!ready || pending || unavailable(stem.id)}
			title={chipTip(stem.id, stem.label)}
			aria-label={`${stem.label} stem mute; Shift+click solo`}
			data-unavailable={unavailable(stem.id)}
			aria-pressed={deck.stems.controls[stem.id].muted}
			data-performance-control={`stem-${stem.id}`}
			data-muted={deck.stems.controls[stem.id].muted}
			data-solo={deck.stems.controls[stem.id].solo}
			style={`--chip-color:${stem.colorVar}`}
			onclick={async (event) => await toggle(event, stem.id)}
		>
			{stem.label}
		</button>
	{/each}
</div>

<style>
	.stems {
		display: flex;
		align-items: center;
		gap: 4px;
		flex: 0 0 auto;
	}
	.mute {
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
		font-weight: 600;
		margin-right: 2px;
	}
	.chip {
		background: color-mix(in srgb, var(--chip-color) 15%, transparent);
		border: 1px solid var(--chip-color);
		border-radius: 2px;
		color: var(--chip-color);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		font-weight: 600;
		padding: 1px 8px;
		cursor: pointer;
	}
	.chip.muted {
		background: transparent;
		color: var(--rb-text-dim);
		border-color: var(--rb-border);
		text-decoration: line-through;
	}
	.chip.solo {
		box-shadow: 0 0 6px var(--chip-color);
	}
	.chip:disabled {
		background: transparent;
		color: var(--rb-text-dim);
		border-color: var(--rb-border);
		cursor: default;
		opacity: 0.6;
	}
	/* Dimmer than merely-disabled: this control does not exist for this
	   bundle, as opposed to existing but not being ready yet. */
	.chip.unavailable {
		opacity: 0.3;
		border-style: dashed;
		cursor: not-allowed;
	}
</style>
