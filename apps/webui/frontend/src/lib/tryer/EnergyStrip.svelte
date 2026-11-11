<script lang="ts">
	import type { TryEnergyBlock } from './tryer-model';

	let { block }: { block: TryEnergyBlock } = $props();

	const ready = $derived(
		!block.unscored &&
			block.out !== null &&
			block.in !== null &&
			block.overlap_out_start_bar !== null
	);

	function overlapStyle(side: 'out' | 'in'): string {
		if (!ready || block.out === null || block.in === null) return '0%';
		const row = side === 'out' ? block.out : block.in;
		const start = side === 'out' ? block.overlap_out_start_bar! : block.overlap_in_start_bar!;
		const end = side === 'out' ? block.overlap_out_end_bar! : block.overlap_in_end_bar!;
		const left = ((start - row.start_bar) / row.levels.length) * 100;
		const width = ((end - start + 1) / row.levels.length) * 100;
		return `left: ${left}%; width: ${width}%`;
	}

	function tickStyle(side: 'out' | 'in', bar: number): string {
		const row = side === 'out' ? block.out : block.in;
		if (row === null) return '0%';
		const index = bar - row.start_bar;
		if (index < 0 || index >= row.levels.length) return '0%';
		return `left: ${((index + 0.5) / row.levels.length) * 100}%`;
	}

	function levelHeight(level: number): string {
		return `${(level / 10) * 100}%`;
	}
</script>

{#if ready && block.out !== null && block.in !== null}
	<div class="strip" aria-label="MIK energy levels over each deck window; overlap shaded">
		{#if block.mik_grid_suspect && block.grid_warning !== null}
			<span class="warn" title={block.grid_warning}>!</span>
		{/if}
		{#each ['out', 'in'] as deck (deck)}
			{@const row = deck === 'out' ? block.out : block.in}
			<div class="row" data-deck={deck}>
				<span class="label" title={deck === 'out' ? 'Outgoing MIK levels' : 'Incoming MIK levels'}
					>{deck}</span
				>
				<div
					class="track"
					title="Bar {row.start_bar} through {row.start_bar + row.levels.length - 1}"
				>
					<div class="overlap" style={overlapStyle(deck as 'out' | 'in')}></div>
					{#each row.levels as level, index (index)}
						<div
							class="seg"
							style:height={levelHeight(level)}
							title="MIK level {level} at bar {row.start_bar + index}"
						></div>
					{/each}
					{#each block.boundaries.filter((b) => b.deck === deck) as boundary (boundary.bar)}
						<span
							class="tick"
							style:left={tickStyle(deck as 'out' | 'in', boundary.bar)}
							title="MIK boundary at bar {boundary.bar}"
						></span>
					{/each}
				</div>
			</div>
		{/each}
	</div>
{/if}

<style>
	.strip {
		position: relative;
		display: grid;
		gap: 0.25rem;
		margin-top: 0.35rem;
	}
	.warn {
		position: absolute;
		top: 0;
		right: 0;
		color: var(--danger, #e44);
		font-weight: 700;
		cursor: help;
	}
	.row {
		display: grid;
		grid-template-columns: 2rem 1fr;
		gap: 0.35rem;
		align-items: stretch;
	}
	.label {
		font-size: 0.75em;
		text-transform: uppercase;
		opacity: 0.75;
	}
	.track {
		position: relative;
		display: flex;
		align-items: flex-end;
		gap: 1px;
		height: 2.5rem;
		background: rgba(255, 255, 255, 0.04);
		border-radius: 3px;
	}
	.overlap {
		position: absolute;
		top: 0;
		bottom: 0;
		background: rgba(90, 160, 255, 0.18);
		pointer-events: none;
		border-radius: 2px;
	}
	.seg {
		flex: 1 1 0;
		min-width: 2px;
		background: rgba(255, 180, 70, 0.85);
		border-radius: 1px 1px 0 0;
		align-self: flex-end;
	}
	.row[data-deck='out'] .seg {
		background: rgba(90, 160, 255, 0.85);
	}
	.tick {
		position: absolute;
		bottom: 0;
		width: 2px;
		height: 100%;
		transform: translateX(-50%);
		background: rgba(255, 255, 255, 0.75);
		pointer-events: none;
	}
</style>
