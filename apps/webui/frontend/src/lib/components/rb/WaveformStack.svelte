<script lang="ts">
	// Build unit: wavestack (COMPONENT-MAP 1.2). Stub - owner replaces the body.
	// 4 rows, one per deck (1-4). Data: /anlz waveform.detail + beatgrid +
	// cues + phrases; playhead from the audio engine. Empty deck = blank row,
	// never an invented waveform.
	import type { DeckId } from '$lib/rb/types';

	const deckIds: DeckId[] = [1, 2, 3, 4];
</script>

<div class="rb-wavestack">
	{#each deckIds as deckId (deckId)}
		<div class="rb-waverow rb-panel">
			<span class="gutter">{deckId}</span>
			<span class="label">WaveformStack row stub - build unit: wavestack</span>
		</div>
	{/each}
</div>

<style>
	.rb-wavestack {
		grid-area: wavestack;
		display: grid;
		grid-template-rows: repeat(4, var(--rb-waverow-h));
	}
	.rb-waverow {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 0 6px;
		color: var(--rb-text-dim);
	}
	.gutter {
		color: var(--rb-text);
		font-size: var(--rb-fs-deck-title);
		width: 16px;
	}
	.label {
		font-size: var(--rb-fs-label);
	}
</style>
