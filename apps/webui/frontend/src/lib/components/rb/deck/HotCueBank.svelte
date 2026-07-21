<script lang="ts">
	// 8-slot hot-cue bank A-H as WIDE slot rows, 2 columns x 4 rows (A-D
	// left, E-H right, SCREENSHOT-SPEC 3 wide layout): this bank is the
	// deck panel's flexible middle and fills all spare width. Populated
	// slot click = real jump to in_ms via the audio engine (COMPONENT-MAP
	// 1.3); empty slots render dim + disabled. HOT CUE dropdown selector
	// below-left is visual-only (inert).
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

	function fmtMs(ms: number): string {
		const total = Math.floor(ms / 1000);
		const m = Math.floor(total / 60);
		const s = total % 60;
		return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
	}
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
				<span class="letter">{entry.slot}</span>
				{#if entry.cue !== null}
					<span class="cue-label">{entry.cue.comment ?? `CUE ${entry.slot}`}</span>
					<span class="cue-time">{fmtMs(entry.cue.in_ms)}</span>
				{/if}
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
		min-width: 0;
		flex: 1 1 auto;
		width: 100%;
	}
	.bank {
		display: grid;
		grid-template-rows: repeat(4, minmax(18px, 1fr));
		grid-template-columns: repeat(2, minmax(0, 1fr));
		grid-auto-flow: column;
		gap: 3px;
		flex: 1 1 auto;
		min-height: 0;
	}
	.slot {
		display: flex;
		align-items: center;
		gap: 6px;
		min-width: 0;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		font-weight: 600;
		text-align: left;
		padding: 1px 6px;
		opacity: 0.55;
		cursor: default;
	}
	.slot .letter {
		flex: 0 0 auto;
	}
	.slot.filled {
		opacity: 1;
		color: var(--rb-text);
		border-left: 3px solid var(--rb-green);
		cursor: pointer;
	}
	.slot.filled .letter {
		color: var(--rb-green);
	}
	.slot.filled.loop {
		border-left-color: var(--rb-orange);
	}
	.slot.filled.loop .letter {
		color: var(--rb-orange);
	}
	.cue-label {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-weight: 400;
	}
	.cue-time {
		flex: 0 0 auto;
		color: var(--rb-text-dim);
		font-weight: 400;
		font-variant-numeric: tabular-nums;
	}
	.dropdown {
		align-self: flex-start;
	}
	.caret {
		color: var(--rb-text-dim);
	}
</style>
