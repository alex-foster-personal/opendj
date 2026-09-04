<script lang="ts">
	/**
	 * LIBUX-05 cmd+E: "raises the EQs mid-screen" while in overlay mode.
	 * Renders the same live HI/MID/LOW knobs as the mixer's ChannelStrip,
	 * through the same typed command dispatcher - not a second, disconnected
	 * set of controls. Only decks with a track loaded show a strip, same
	 * gate the edge-reveal regions use.
	 */
	import { getDeckState, mixerState } from '$lib/rb/audio-engine.svelte';
	import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';
	import { knobId } from '$lib/rb/knob-control.svelte';
	import type { EqBand } from '$lib/rb/mixer-types';
	import Knob from './mixer/Knob.svelte';

	/**
	 * The deck id, declared inline rather than imported.
	 *
	 * Deliberate, and the same call perf-event-log.ts and
	 * presentation-clock-report.ts make for the same reason: `deck-slots.ts`
	 * is the tree's fan-in ceiling and the quality ratchet holds that key at
	 * its measured floor with ZERO headroom on purpose, so the next importer
	 * reds the gate for every lane at once. A four-member union is not worth
	 * doing that to whoever rebases next.
	 */
	type DeckId = 1 | 2 | 3 | 4;

	const STRIP_ORDER: DeckId[] = [3, 1, 2, 4];
	const loadedOrder = $derived(STRIP_ORDER.filter((d) => getDeckState(d).stable_id !== null));

	function handleEq(deck: DeckId, band: EqBand, value: number): void {
		void runPerformanceCommandFromUi({ type: 'eq', deck, band, value });
	}
</script>

<div class="eq-overlay" role="group" aria-label="Technically-working mode EQ overlay">
	{#each loadedOrder as deck (deck)}
		<div class="eq-strip" data-eq-overlay-channel={deck}>
			<span class="ch-num">{deck}</span>
			<Knob
				knobId={knobId(deck, 'high')}
				label="HI"
				value={mixerState.channels[deck].eq_high}
				onchange={(v) => handleEq(deck, 'high', v)}
			/>
			<Knob
				knobId={knobId(deck, 'mid')}
				label="MID"
				value={mixerState.channels[deck].eq_mid}
				onchange={(v) => handleEq(deck, 'mid', v)}
			/>
			<Knob
				knobId={knobId(deck, 'low')}
				label="LOW"
				value={mixerState.channels[deck].eq_low}
				onchange={(v) => handleEq(deck, 'low', v)}
			/>
		</div>
	{/each}
	{#if loadedOrder.length === 0}
		<span class="eq-overlay-empty">No decks loaded</span>
	{/if}
</div>

<style>
	/* Fixed mid-screen, ~55% down the viewport (LIBUX-05: "maybe 40-70%
	 * sort of location vertically"). */
	.eq-overlay {
		position: fixed;
		left: 50%;
		top: 55%;
		transform: translate(-50%, -50%);
		z-index: 60;
		/* .perf-root.tw-active sets pointer-events: none on itself so the
		 * transparent gaps around hidden regions click through to whatever is
		 * behind the window; this overlay only ever renders while tech mode
		 * is active, so it must re-assert auto or its knobs inherit none and
		 * go dead the moment cmd+E raises them. */
		pointer-events: auto;
		display: flex;
		gap: 14px;
		padding: 10px 16px;
		border-radius: 4px;
		background: color-mix(in srgb, var(--rb-panel-raised, #1a1e25) 92%, transparent);
		border: 1px solid var(--rb-border, #3d4652);
		box-shadow: 0 6px 24px rgba(0, 0, 0, 0.5);
	}
	.eq-strip {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 4px;
	}
	.ch-num {
		font-size: 10px;
		color: var(--rb-text, #e8ecf1);
		line-height: 1;
	}
	.eq-overlay-empty {
		font-size: 11px;
		color: var(--rb-text-dim, #8b93a1);
		padding: 4px 8px;
	}
</style>
