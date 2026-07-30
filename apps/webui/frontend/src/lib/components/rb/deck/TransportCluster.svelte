<script lang="ts">
	// Transport (SCREENSHOT-SPEC 3): round CUE button over round play/pause.
	// Both REAL via the audio engine (COMPONENT-MAP 1.3) - CUE has
	// return-to-cue semantics, play toggles transport. Disabled while no
	// track is loaded (engine throws on empty decks; we never swallow that
	// by pretending to play).
	import type { DeckState } from '$lib/rb/types';
	import ControlExplainer from './ControlExplainer.svelte';

	let {
		deck,
		pending,
		onCue,
		onPlayPause
	}: {
		deck: DeckState;
		pending: boolean;
		onCue: () => Promise<void>;
		onPlayPause: () => Promise<void>;
	} = $props();

	const hasTrack: boolean = $derived(deck.stable_id !== null);

	// Copy mirrors audio-engine pressCue / pause - click only, no hold-to-preview.
	const cueTitle: string = $derived(
		!hasTrack
			? 'no track loaded'
			: deck.playing
				? 'CUE - return to cue and pause'
				: deck.cue_ms === null
					? 'CUE - set cue at playhead'
					: 'CUE - jump playhead to cue'
	);
	const cueBullets: readonly string[] = [
		'Pause stores the memory cue at the pause point (quantized when Q is on).',
		'While playing: jump to that cue (or track start if unset) and pause.',
		'While paused: jump to the cue, or set it if none exists yet.',
		'Separate from hot cues A-H. Click only - no hold-to-preview.'
	];
	const playTitle: string = $derived(
		!hasTrack
			? 'no track loaded'
			: deck.playing
				? 'Pause - also stores the memory cue here'
				: 'Play from current playhead'
	);
</script>

<div class="transport">
	<ControlExplainer title={cueTitle} bullets={cueBullets} demo="cue">
		<button
			class="round cue"
			disabled={!hasTrack || pending}
			title={cueTitle}
			aria-label="cue"
			data-performance-control="cue"
			onclick={async () => await onCue()}
		>
			CUE
		</button>
	</ControlExplainer>
	<button
		class="round play"
		class:playing={deck.playing}
		disabled={!hasTrack || pending}
		title={playTitle}
		aria-label={deck.playing ? 'pause' : 'play'}
		aria-pressed={deck.playing}
		data-performance-control="play"
		data-state={deck.playing ? 'on' : 'off'}
		onclick={async () => await onPlayPause()}
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
		padding: 0;
	}
	.round:disabled {
		opacity: 0.35;
		cursor: not-allowed;
	}
	.round.cue:hover:not(:disabled) {
		border-color: var(--rb-accent);
	}
	.round.playing {
		color: var(--rb-accent);
		border-color: var(--rb-accent);
	}
	.glyph {
		width: 16px;
		height: 16px;
	}
</style>
