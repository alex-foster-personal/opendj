<script lang="ts">
	// Transport (SCREENSHOT-SPEC 3): round CUE button over round play/pause.
	// Both REAL via the audio engine (COMPONENT-MAP 1.3) - CUE has
	// return-to-cue semantics, play toggles transport. Disabled while no
	// track is loaded (engine throws on empty decks; we never swallow that
	// by pretending to play).
	import type { DeckState } from '$lib/rb/types';

	let {
		deck,
		onCue,
		onPlayPause
	}: { deck: DeckState; onCue: () => void; onPlayPause: () => void } = $props();

	const hasTrack: boolean = $derived(deck.stable_id !== null);
</script>

<div class="transport">
	<button
		class="round cue"
		disabled={!hasTrack}
		title={hasTrack ? 'return to cue' : 'no track loaded'}
		onclick={onCue}
	>
		CUE
	</button>
	<button
		class="round play"
		class:playing={deck.playing}
		disabled={!hasTrack}
		title={hasTrack ? (deck.playing ? 'pause' : 'play') : 'no track loaded'}
		aria-label={deck.playing ? 'pause' : 'play'}
		onclick={onPlayPause}
	>
		{#if deck.playing}
			<svg viewBox="0 0 16 16" class="glyph" aria-hidden="true">
				<rect x="3.5" y="3" width="3" height="10" fill="currentColor" />
				<rect x="9.5" y="3" width="3" height="10" fill="currentColor" />
			</svg>
		{:else}
			<svg viewBox="0 0 16 16" class="glyph" aria-hidden="true">
				<path d="M4.5 3 L13 8 L4.5 13 Z" fill="currentColor" />
			</svg>
		{/if}
	</button>
</div>

<style>
	.transport {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 6px;
		flex: 0 0 auto;
	}
	.round {
		width: 38px;
		height: 38px;
		border-radius: 50%;
		background: radial-gradient(circle at 50% 35%, #262b33, #101318 75%);
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		font-weight: 600;
		display: flex;
		align-items: center;
		justify-content: center;
		cursor: pointer;
	}
	.round:disabled {
		color: var(--rb-text-dim);
		cursor: default;
		opacity: 0.6;
	}
	.round.cue:not(:disabled):active {
		border-color: var(--rb-orange);
		color: var(--rb-orange);
	}
	.round.play.playing {
		border-color: var(--rb-accent);
		color: var(--rb-accent);
		box-shadow: 0 0 6px var(--rb-accent-glow);
	}
	.glyph {
		width: 14px;
		height: 14px;
		display: block;
	}
</style>
