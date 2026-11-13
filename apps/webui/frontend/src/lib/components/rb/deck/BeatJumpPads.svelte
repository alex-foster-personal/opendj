<script lang="ts">
	// Beat Jump pad mode (DECKUX-9956): the cue bank's eight pads become
	// signed beat jumps while the deck's pad mode is Beat Jump. A-D (left
	// column) jump back 1, 2, 4, 8 beats, E-H (right column) forward 1, 2, 4,
	// 8, mimicking rekordbox's BEAT JUMP pads. Each press sends the shared
	// typed `beat_jump` command, so quantize, phase and the end clamp are the
	// engine's. Inert with a reason when no track or no beatgrid is loaded.
	import { BEAT_JUMP_PADS, beatJumpPadLabel } from '$lib/rb/beat-jump-pads';
	import type { DeckState } from '$lib/rb/deck-state-types';
	import { selectorNode } from '$lib/keyboard/selector-register';
	import { deckPadSelId, padNumberForSlot } from './deck-pad-selector-ids';

	let {
		deck,
		pending,
		onJump
	}: {
		deck: DeckState;
		pending: boolean;
		onJump: (beats: number) => Promise<void>;
	} = $props();

	const blockedReason: string | null = $derived(
		pending
			? 'deck command pending'
			: deck.stable_id === null
				? 'no track loaded'
				: (deck.anlz?.beatgrid.beats ?? []).length < 2
					? 'track has no usable beatgrid - beat jump unavailable'
					: null
	);
</script>

<div class="beat-jump-pads" role="group" aria-label={`beat jump pads deck ${deck.deck_id}`}>
	{#each [BEAT_JUMP_PADS.slice(0, 4), BEAT_JUMP_PADS.slice(4, 8)] as column, columnIndex (columnIndex)}
		<div class="pad-col">
			{#each column as pad (pad.slot)}
				<button
					type="button"
					class="pad"
					class:back={pad.beats < 0}
					disabled={blockedReason !== null}
					use:selectorNode={{
						id: deckPadSelId(deck.deck_id, padNumberForSlot(pad.slot)),
						kind: 'leaf',
						enabled: blockedReason === null
					}}
					aria-label={`beat jump ${beatJumpPadLabel(pad.beats)} deck ${deck.deck_id}`}
					data-testid={`beat-jump-pad-${deck.deck_id}-${pad.slot}`}
					data-beats={pad.beats}
					data-performance-control="beat-jump-pad"
					title={blockedReason ??
						`jump ${pad.beats < 0 ? 'back' : 'forward'} ${Math.abs(pad.beats)} beat${Math.abs(pad.beats) === 1 ? '' : 's'}`}
					onclick={() => void onJump(pad.beats)}
				>
					<span class="letter">{pad.slot}</span>
					<span class="beats">{beatJumpPadLabel(pad.beats)}</span>
				</button>
			{/each}
		</div>
	{/each}
</div>

<style>
	.beat-jump-pads {
		display: flex;
		gap: 4px;
		width: 100%;
	}
	.pad-col {
		display: flex;
		flex-direction: column;
		gap: 3px;
		flex: 1 1 0;
	}
	.pad {
		display: flex;
		align-items: center;
		gap: 6px;
		height: 18px;
		box-sizing: border-box;
		padding: 0 6px;
		background: var(--rb-panel);
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		color: var(--rb-text);
		font-size: var(--rb-fs-label, 10px);
		cursor: pointer;
	}
	.pad:hover:not(:disabled) {
		background: rgba(255, 255, 255, 0.06);
	}
	.pad:disabled {
		opacity: 0.55;
		cursor: default;
	}
	.letter {
		color: var(--rb-text-dim);
	}
	.beats {
		font-variant-numeric: tabular-nums;
	}
</style>
