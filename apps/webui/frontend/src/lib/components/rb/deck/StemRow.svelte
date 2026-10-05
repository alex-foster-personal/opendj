<script lang="ts">
	// Real precomputed Demucs controls. Click toggles mute; Shift+click toggles
	// solo. Both paths dispatch through the typed browser-agent command surface.
	import type { DeckState } from '$lib/rb/deck-state-types';
	import { STEM_COLORS } from '$lib/rb/stem-colors';
	import { stemStatusView } from '$lib/rb/stem-status';
	import type { StemControl } from '$lib/rb/stem-types';

	let {
		deck,
		pending,
		testIdScope = 'deck',
		onMute,
		onSolo,
		onLoad
	}: {
		deck: DeckState;
		pending: boolean;
		/** Namespace for chip testids: `deck` keeps `stem-*-deck-N`; mixer uses `channel`. */
		testIdScope?: 'deck' | 'channel';
		onMute: (stem: StemControl) => Promise<void>;
		onSolo: (stem: StemControl) => Promise<void>;
		/** STEM-48: retry a failed stem load, or start one that is held. The deck
		 * passes it; the mixer strip does not, and shows the reason on hover only. */
		onLoad?: () => Promise<void>;
	} = $props();

	const STEMS: readonly { id: StemControl; label: string; color: string }[] = [
		{ id: 'vocal', label: 'VOCAL', color: STEM_COLORS.vocal },
		{ id: 'instrumental', label: 'INST', color: STEM_COLORS.instrumental },
		{ id: 'drums', label: 'DRUMS', color: STEM_COLORS.drums },
		{ id: 'bass', label: 'BASS', color: STEM_COLORS.bass },
		{ id: 'other', label: 'HARM', color: STEM_COLORS.other }
	];
	const visibleStems = $derived(STEMS.filter((stem) =>
		(stem.id !== 'bass' && stem.id !== 'other') || deck.stems.available_controls.includes(stem.id)));
	const ready: boolean = $derived(deck.stems.status === 'ready');

	/** A control this bundle's layout cannot drive. RoFormer's 2-stem split
	 * folds the drums into `instrumental`, so DRUMS has no signal of its own:
	 * the chip renders inert with a reason rather than accepting a click that
	 * could never change what you hear. */
	function unavailable(stem: StemControl): boolean {
		return ready && !deck.stems.available_controls.includes(stem);
	}

	/** STEM-48: the one named reading of the stem state. A chip that is not
	 * live always carries this reason on hover, and the deck shows its label. */
	const view = $derived(stemStatusView(deck.stems));
	const statusTip: string = $derived(view.tip);

	function chipTip(stem: StemControl, label: string): string {
		if (unavailable(stem)) {
			return (
				`${label} is not a separate stem in this ${deck.stems.layout ?? 'bundle'} ` +
				`- it is mixed into INST, so it cannot be muted on its own`
			);
		}
		const ctrl = deck.stems.controls[stem];
		const state = ctrl.solo ? 'solo selected' : ctrl.muted ? 'muted now' : 'unmuted (other solo/group controls still apply)';
		return `${label} stem is ${state}. Click to mute, Shift+click to solo. ${statusTip}`;
	}

	async function toggle(event: MouseEvent, stem: StemControl): Promise<void> {
		if (event.shiftKey) await onSolo(stem);
		else await onMute(stem);
	}
</script>

	<div class="stems" role="group" aria-label={`stem controls deck ${deck.deck_id}`} data-stems-status={deck.stems.status} data-stems-state={view.name} title={statusTip}>
	<span class="mute">MUTE</span>
	{#each visibleStems as stem (stem.id)}
		<button
			class="chip"
			class:muted={deck.stems.controls[stem.id].muted}
			class:solo={deck.stems.controls[stem.id].solo}
			class:unavailable={unavailable(stem.id)}
			disabled={!ready || unavailable(stem.id)}
			aria-busy={pending}
			title={chipTip(stem.id, stem.label)}
			aria-label={`${stem.label} stem mute deck ${deck.deck_id}; Shift+click solo`}
			data-testid={`stem-${stem.id}-${testIdScope}-${deck.deck_id}`}
			data-unavailable={unavailable(stem.id)}
			aria-pressed={deck.stems.controls[stem.id].muted}
			data-performance-control={`stem-${stem.id}`}
			data-muted={deck.stems.controls[stem.id].muted}
			data-solo={deck.stems.controls[stem.id].solo}
			style={`--chip-color:${stem.color}`}
			onclick={async (event) => await toggle(event, stem.id)}
		>
			{testIdScope === 'channel' ? stem.label.slice(0, 3) : stem.label}
		</button>
	{/each}
	{#if onLoad !== undefined && view.label !== ''}
		{#if view.action !== null}
			<button
				class="status action"
				class:failed={view.name === 'error'}
				title={view.tip}
				aria-label={`${view.tip} Deck ${deck.deck_id}.`}
				data-testid={`stem-status-${testIdScope}-${deck.deck_id}`}
				data-stems-state={view.name}
				data-stems-action={view.action}
				data-performance-control="stem-load"
				onclick={async () => await onLoad()}
			>
				{view.label}
			</button>
		{:else}
			<span
				class="status"
				class:busy={view.busy}
				role="status"
				title={view.tip}
				data-testid={`stem-status-${testIdScope}-${deck.deck_id}`}
				data-stems-state={view.name}
			>
				{view.label}
			</span>
		{/if}
	{/if}
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
	/* STEM-48: names what the stems are doing whenever the chips are not live.
	   Truncates rather than pushing the deck's row wider; the hover title
	   carries the whole sentence. */
	.status {
		max-width: 190px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		font-weight: 600;
		color: var(--rb-text-dim);
		margin-left: 4px;
	}
	.status.busy {
		color: var(--rb-blue, #4aa3ff);
	}
	.status.action {
		background: transparent;
		border: 1px solid var(--rb-blue, #4aa3ff);
		border-radius: 2px;
		color: var(--rb-blue, #4aa3ff);
		padding: 1px 6px;
		cursor: pointer;
	}
	.status.action.failed {
		border-color: var(--rb-red, #d0342c);
		color: #ff8e87;
	}
	/* Dimmer than merely-disabled: this control does not exist for this
	   bundle, as opposed to existing but not being ready yet. */
	.chip.unavailable {
		opacity: 0.3;
		border-style: dashed;
		cursor: not-allowed;
	}
</style>
