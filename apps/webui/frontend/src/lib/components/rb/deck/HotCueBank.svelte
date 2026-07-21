<script lang="ts">
	// 8-slot hot-cue bank A-H, 2 columns x 4 rows (A-D left, E-H right,
	// SCREENSHOT-SPEC 3). Populated slot click = real jump to in_ms via the
	// audio engine (COMPONENT-MAP 1.3); empty slots render dim + disabled.
	// HOT CUE dropdown selector below-left is visual-only (inert).
	import type { DeckState, HotCue, HotCueSlot } from '$lib/rb/types';

	let {
		deck,
		onJump,
		inertTip
	}: { deck: DeckState; onJump: (ms: number) => void; inertTip: string } = $props();

	const SLOTS: HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

	const bank: { slot: HotCueSlot; cue: HotCue | null }[] = $derived(
		SLOTS.map((slot) => ({
			slot,
			cue: deck.hot_cues.find((c) => c.slot === slot) ?? null
		}))
	);
</script>

<div class="cue-area">
	<div class="bank">
		{#each bank as entry (entry.slot)}
			<button
				class="slot"
				class:filled={entry.cue !== null}
				class:loop={entry.cue !== null && entry.cue.is_loop}
				disabled={entry.cue === null}
				title={entry.cue === null
					? 'empty hot cue slot'
					: (entry.cue.comment ?? `hot cue ${entry.slot}`)}
				onclick={() => {
					if (entry.cue !== null) onJump(entry.cue.in_ms);
				}}
			>
				{entry.slot}
			</button>
		{/each}
	</div>
	<button class="rb-lit-button rb-inert dropdown" disabled title={inertTip}>
		HOT CUE <span class="caret">&#9662;</span>
	</button>
</div>

<style>
	.cue-area {
		display: flex;
		flex-direction: column;
		gap: 3px;
		min-height: 0;
	}
	.bank {
		display: grid;
		grid-template-rows: repeat(4, 1fr);
		grid-template-columns: repeat(2, 1fr);
		grid-auto-flow: column;
		gap: 2px;
		flex: 1 1 auto;
		min-height: 0;
	}
	.slot {
		min-width: 34px;
		min-height: 16px;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		font-weight: 600;
		text-align: left;
		padding: 1px 4px;
		opacity: 0.55;
		cursor: default;
	}
	.slot.filled {
		opacity: 1;
		color: var(--rb-text);
		border-left: 3px solid var(--rb-green);
		cursor: pointer;
	}
	.slot.filled.loop {
		border-left-color: var(--rb-orange);
	}
	.slot.filled:active {
		background: var(--rb-select);
	}
	.dropdown {
		align-self: flex-start;
	}
	.caret {
		color: var(--rb-text-dim);
	}
</style>
