<script lang="ts">
	// Transport (SCREENSHOT-SPEC 3): round CUE button over round play/pause.
	// Both REAL via the audio engine (COMPONENT-MAP 1.3) - CUE has
	// return-to-cue semantics, play toggles transport. Disabled while no
	// track is loaded (engine throws on empty decks; we never swallow that
	// by pretending to play).
	import type { DeckState } from '$lib/rb/deck-state-types';
	import { QUANTIZED_LAUNCH } from '$lib/player/transport/quantized-launch';
	import ControlExplainer from './ControlExplainer.svelte';

	let {
		deck,
		pending,
		quantizedLaunchArmed,
		onCue,
		onPlayPause
	}: {
		deck: DeckState;
		pending: boolean;
		quantizedLaunchArmed: { remaining_ms: number; launch_at_context_sec: number } | null;
		/**
		 * Q1: both take the ORIGINATING event's own `event.timeStamp`, on the
		 * `performance.now()` epoch, and the parameter exists so that the stamp
		 * is never re-taken further down. A `performance.now()` read inside the
		 * handler starts the clock AFTER the browser has already queued the
		 * input and dispatched to us, and that gap is time the operator felt.
		 */
		onCue: (pressT0Ms?: number) => Promise<void>;
		onPlayPause: (pressT0Ms?: number, quantize?: boolean) => Promise<void>;
	} = $props();

	const hasTrack: boolean = $derived(deck.stable_id !== null);
	const armed: boolean = $derived(quantizedLaunchArmed !== null);
	const armedCountdownLabel: string = $derived(
		armed ? `${(quantizedLaunchArmed!.remaining_ms / 1000).toFixed(1)}s` : ''
	);

	// LATENCY-01 visual feedback: `pending` must NOT reach `disabled`. It used
	// to, and the measured mutation sequence for a single click was
	// disabled@1.8ms -> data-state@2.8ms -> disabled@6.2ms: the button greyed
	// out roughly one frame BEFORE it flipped, then came back. On a 60Hz display
	// that renders as a blink on every press, and a control that flickers reads
	// as slower than one that does not, whatever the clock says. Re-entrancy is
	// already handled where it belongs - the command dispatcher serializes per
	// deck-and-sync scope - not by making the button unclickable for 10ms.
	// `aria-busy` keeps the in-flight state observable to agents and assistive
	// tech without touching layout, paint or interactivity.

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
			: armed
				? `${QUANTIZED_LAUNCH} in ${armedCountdownLabel}`
				: deck.playing
					? 'Pause - also stores the memory cue here'
					: 'Play from current playhead'
	);
</script>

<div class="transport">
	<ControlExplainer title={cueTitle} bullets={cueBullets} demo="cue">
		<button
			class="round cue"
			disabled={!hasTrack}
			aria-busy={pending}
			title={cueTitle}
			aria-label={`cue deck ${deck.deck_id}`}
			data-testid={`cue-deck-${deck.deck_id}`}
			data-performance-control="cue"
			onclick={async (event) => await onCue(event.timeStamp)}
		>
			CUE
		</button>
	</ControlExplainer>
	<button
		class="round play"
		class:playing={deck.playing}
		class:armed
		disabled={!hasTrack}
		aria-busy={pending}
		title={playTitle}
		aria-label={armed ? `${QUANTIZED_LAUNCH} deck ${deck.deck_id} in ${armedCountdownLabel}` : `play deck ${deck.deck_id}`}
		aria-pressed={deck.playing}
		data-testid={`play-deck-${deck.deck_id}`}
		data-performance-control="play"
		data-state={armed ? 'armed' : deck.playing ? 'on' : 'off'}
		onclick={async (event) =>
			await onPlayPause(event.timeStamp, event.metaKey || event.ctrlKey)}
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
			{#if armed}
				<span class="countdown" title="Remaining time until QUANTIZED LAUNCH">{armedCountdownLabel}</span>
			{/if}
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
	.round.armed {
		color: var(--rb-accent);
		border-color: var(--rb-accent);
		box-shadow: 0 0 0 1px color-mix(in srgb, var(--rb-accent) 45%, transparent);
	}
	.glyph {
		width: 16px;
		height: 16px;
	}
	.countdown {
		position: absolute;
		bottom: -14px;
		font-size: 10px;
		line-height: 1;
		color: var(--rb-accent);
	}
	.round.play {
		position: relative;
	}
</style>
