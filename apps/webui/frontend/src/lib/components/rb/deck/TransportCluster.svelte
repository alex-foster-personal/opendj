<script lang="ts">
	// Transport (SCREENSHOT-SPEC 3): round CUE button over round play/pause.
	// Both REAL via the audio engine (COMPONENT-MAP 1.3) - CUE has
	// return-to-cue semantics, play toggles transport. Disabled while no
	// track is loaded (engine throws on empty decks; we never swallow that
	// by pretending to play).
	import type { DeckState } from '$lib/rb/deck-state-types';
	import { QUANTIZED_LAUNCH } from '$lib/player/transport/quantized-launch';
	import { unsyncedPlayWarning } from '$lib/rb/beat-sync-math';
	import ControlExplainer from './ControlExplainer.svelte';
	import { TOOLTIP_ISOLATED } from '$lib/ui/tooltip-timing';
	import { selectorNode } from '$lib/keyboard/selector-register';
	import { transportSelId } from './deck-transport-jog-selector-ids';

	let {
		deck,
		pending,
		quantizedLaunchArmed,
		onCue,
		onMemoryCueCall,
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
		/** CUES-10013: CALL < / > step through the track's memory cues. */
		onMemoryCueCall: (direction: 'prev' | 'next', pressT0Ms?: number) => Promise<void>;
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
	// DECKGAP-7691c: the deck started (or runs) without its phase-lock. Red glow
	// plus a red hover warning; never a refusal (PLAY-27 starts it unsynced).
	const syncWarning: string | null = $derived(
		hasTrack && deck.sync_error !== null ? unsyncedPlayWarning(deck.sync_error) : null
	);
	const playTitle: string = $derived(
		!hasTrack
			? 'no track loaded'
			: armed
				? `${QUANTIZED_LAUNCH} in ${armedCountdownLabel}`
				: `${syncWarning === null ? '' : `${syncWarning} - `}${
						deck.playing ? 'Pause - also stores the memory cue here' : 'Play from current playhead'
					}`
	);
</script>

<!-- #14331: each button sits in its own selector leaf under deck.<n>.transport, so
     Enter on a leaf presses that button. A leaf leaves the tree while its button is disabled. -->
<div class="transport">
	<div class="sel-leaf" use:selectorNode={{ id: transportSelId(deck.deck_id, 'cue'), kind: 'leaf', enabled: hasTrack }}>
	<ControlExplainer title={cueTitle} bullets={cueBullets} demo="cue" tooltip={TOOLTIP_ISOLATED}>
		<button
			class="round cue"
			disabled={!hasTrack}
			aria-busy={pending}
			aria-label={`cue deck ${deck.deck_id}`}
			data-testid={`cue-deck-${deck.deck_id}`}
			data-performance-control="cue"
			onclick={async (event) => await onCue(event.timeStamp)}
		>
			CUE
		</button>
	</ControlExplainer>
	</div>
	<div class="call" role="group" aria-label={`memory cue call deck ${deck.deck_id}`}>
		{#each [['prev', '<', 'CALL < - previous memory cue'], ['next', '>', 'CALL > - next memory cue']] as const as [direction, glyph, title] (direction)}
			<div class="sel-leaf" use:selectorNode={{ id: transportSelId(deck.deck_id, direction === 'prev' ? 'call-prev' : 'call-next'), kind: 'leaf', enabled: hasTrack }}>
			<button
				class="call-btn"
				disabled={!hasTrack}
				{title}
				aria-label={`${title} deck ${deck.deck_id}`}
				data-testid={`memory-cue-call-${direction}-deck-${deck.deck_id}`}
				data-performance-control="memory_cue_call"
				onclick={async (event) => await onMemoryCueCall(direction, event.timeStamp)}
			>{glyph}</button>
			</div>
		{/each}
	</div>
	<div class="sel-leaf" use:selectorNode={{ id: transportSelId(deck.deck_id, 'play'), kind: 'leaf', enabled: hasTrack }}>
	<button
		class="round play"
		class:playing={deck.playing}
		class:armed
		class:sync-warning={syncWarning !== null}
		disabled={!hasTrack}
		aria-busy={pending}
		title={playTitle}
		data-sync-warning={syncWarning ?? undefined}
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
		{#if syncWarning !== null}
			<span class="sync-warning-tip" role="tooltip">{syncWarning}</span>
		{/if}
	</button>
	</div>
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
	/* A column flex box sizes to its one button and keeps it stretched as before. */
	.sel-leaf {
		display: flex;
		flex-direction: column;
	}
	.round {
		width: 38px;
		height: 38px;
		border-radius: 50%;
		background: var(--rb-round-face, radial-gradient(circle at 50% 35%, #262b33, #101318 75%));
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
	.call {
		display: flex;
		gap: 2px;
	}
	.call-btn {
		width: 18px;
		height: 14px;
		padding: 0;
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		background: transparent;
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: 9px;
		line-height: 1;
		cursor: pointer;
	}
	.call-btn:disabled {
		opacity: 0.35;
		cursor: not-allowed;
	}
	.call-btn:hover:not(:disabled) {
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
	.round.play.sync-warning {
		border-color: var(--rb-danger, #ff4d4f);
		box-shadow: 0 0 0 1px var(--rb-danger, #ff4d4f), 0 0 10px 2px color-mix(in srgb, var(--rb-danger, #ff4d4f) 70%, transparent);
	}
	.sync-warning-tip {
		display: none;
		position: absolute;
		left: 50%;
		bottom: calc(100% + 6px);
		transform: translateX(-50%);
		z-index: 80;
		width: max-content;
		max-width: 220px;
		padding: 4px 7px;
		background: #0a0c0f;
		border: 1px solid var(--rb-danger, #ff4d4f);
		border-radius: 3px;
		color: var(--rb-danger, #ff4d4f);
		font-size: 10px;
		font-weight: 600;
		line-height: 1.3;
		pointer-events: none;
	}
	.round.play.sync-warning:hover .sync-warning-tip,
	.round.play.sync-warning:focus-visible .sync-warning-tip {
		display: block;
	}
</style>
