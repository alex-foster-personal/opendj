<script lang="ts">
	/**
	 * Bottom-left acknowledgement that a deck is still pulling SECONDARY assets
	 * after it became playable. Show a small acknowledgement of secondary
	 * loading near the bottom of the deck.
	 *
	 * Deliberately renders NOTHING once the load settles. A badge that lingers
	 * on a finished deck is noise, and four of those on screen is worse noise.
	 *
	 * Deliberately shows NO live-ticking counter either: this sits on all four
	 * decks at once, and a per-frame number here would add exactly the kind of
	 * idle render work the deck loop is already being trimmed for. The stage
	 * durations an agent needs are in the perf ring (`deck-stems` rows).
	 *
	 * `status` is a plain string, not the StemDeckState union, ON PURPOSE:
	 * $lib/rb/types is at its fan-in ceiling (54, per ops/quality/baseline.json)
	 * and importing the type here would make this the 55th importer and trip the
	 * quality ratchet. The parent already holds the typed value and narrows it.
	 */
	const { status, error = null, phase = null }: { status: string; error?: string | null; phase?: string | null } = $props();

	// `unavailable` is a SETTLED answer - this track has no stem bundle - so it
	// gets no badge, the same as `ready`. Only in-flight and failed states show.
	const visible = $derived(status === 'loading' || status === 'error');
	const failed = $derived(status === 'error');
	const hint = $derived(
		failed
			? `Stems unavailable - ${error ?? 'unknown error'}. The deck plays from its mix; stem controls stay off.`
			: `Loading stems in the background${phase === null ? '' : ` (${phase})`}. The deck is already playable - this only gates the VOCAL / INST / DRUMS controls.`
	);
</script>

{#if visible}
	<span
		class="secondary-load"
		class:failed
		data-testid="deck-secondary-load"
		data-stem-load={status}
		data-stem-phase={phase}
		title={hint}
	>
		{failed ? 'STEMS!' : 'STEMS'}
	</span>
{/if}

<style>
	/* Sits in the STEM ROW's own band (16px tall, 4px off the deck bottom), in
	   the gap its MUTE label occupies. Measured on the real deck: MUTE spans
	   x=8..38 and the first stem chip (VOCAL) starts at x=44, so this is capped
	   at 32px and cannot reach the chips. Overlapping the label is deliberate
	   and honest rather than a collision: while the status is `loading` the
	   stem controls are inert -- _setStemControl refuses any non-`ready`
	   status -- so that label has nothing to say yet, and the badge clears
	   itself the moment the load settles.

	   Opaque on purpose. The first attempt was transparent text stacked on the
	   MUTE label and was unreadable. */
	.secondary-load {
		position: absolute;
		left: 6px;
		bottom: 4px;
		height: 16px;
		max-width: 32px;
		z-index: 3;
		box-sizing: border-box;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: 0 3px;
		overflow: hidden;
		border: 1px solid var(--rb-border, #23282f);
		border-radius: 2px;
		background: var(--rb-panel-raised, #1a1e25);
		font-size: 8px;
		line-height: 1;
		letter-spacing: 0.02em;
		white-space: nowrap;
		color: var(--rb-blue, #4aa3ff);
		pointer-events: auto;
		user-select: none;
		/* The pulse IS the "in flight" signal, so it carries the meaning the
		   dot used to. Opacity-only, so it composites off the main thread and
		   four decks pulsing at once cost no layout. */
		animation: secondary-pulse 1.1s ease-in-out infinite;
	}

	.secondary-load.failed {
		color: #ff8e87;
		border-color: var(--rb-red, #d0342c);
		animation: none;
	}

	@keyframes secondary-pulse {
		0%,
		100% {
			opacity: 0.45;
		}
		50% {
			opacity: 1;
		}
	}

	/* The pulse is decoration: the text and the hover title carry the whole
	   message, so the animation is safe to drop. */
	@media (prefers-reduced-motion: reduce) {
		.secondary-load {
			animation: none;
			opacity: 0.9;
		}
	}
</style>
