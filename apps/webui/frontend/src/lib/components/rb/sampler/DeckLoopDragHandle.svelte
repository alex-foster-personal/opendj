<script lang="ts">
	/**
	 * "Drag loop to sampler" chip (spec 7.4 "Drag a loop in", #11127): shown on a deck while it
	 * has an engaged loop and the edge sampler is on. A pointer drag from here wakes the edges,
	 * the tab under the pointer gets the dashed drop outline, and the drop saves a capture slot
	 * there with the deck's EQ, filter and stem settings (sampler-store.svelte.ts captureToSet).
	 */
	import {
		beginLoopDrag,
		endLoopDrag,
		moveLoopDrag
	} from '$lib/player/sampler/sampler-store.svelte';
	import {
		dropTargetOf,
		loopBarsLabel,
		type DropTarget
	} from '$lib/player/sampler/sampler-store-logic';
	import type { DeckId } from '$lib/rb/deck-id';
	import type { DeckState } from '$lib/rb/deck-state-types';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { nativeShellKind } from '$lib/shell/native-shell';
	import './edge-sampler-tokens.css';

	let { deck, deckId }: { deck: DeckState; deckId: DeckId } = $props();

	const TIP =
		'Drag this loop to either screen edge to save it as a sample, with its current EQ, filter and stem settings';
	const shellOk = typeof window !== 'undefined' && nativeShellKind() === 'electron';

	/** The edge tab under the pointer; the tabs carry data-drop-edge / data-drop-index. */
	function tabAt(x: number, y: number): DropTarget | null {
		const el = document.elementFromPoint(x, y)?.closest<HTMLElement>('[data-drop-edge]');
		return el ? dropTargetOf(el.dataset) : null;
	}

	function down(ev: PointerEvent): void {
		ev.stopPropagation();
		ev.preventDefault();
		(ev.currentTarget as HTMLElement).setPointerCapture(ev.pointerId);
		beginLoopDrag(deckId, ev.clientX, ev.clientY);
	}

	function end(ev: PointerEvent, cancelled: boolean): void {
		// A failed capture is toasted inside captureToSet.
		endLoopDrag(cancelled ? null : tabAt(ev.clientX, ev.clientY), cancelled).catch(() => null);
	}
</script>

{#if shellOk && uiPrefs.sampler_enabled && deck.loop?.engaged}
	<button
		type="button"
		class="loop-drag"
		title={TIP}
		aria-label={TIP}
		data-testid="deck-loop-drag-{deckId}"
		onpointerdown={down}
		onpointermove={(ev) => moveLoopDrag(ev.clientX, ev.clientY, tabAt(ev.clientX, ev.clientY))}
		onpointerup={(ev) => end(ev, false)}
		onpointercancel={(ev) => end(ev, true)}
	>
		<svg viewBox="0 0 16 16" aria-hidden="true"
			><path d="M3 7a5 5 0 018.5-3.5L13 5M13 2v3h-3M13 9a5 5 0 01-8.5 3.5L3 11M3 14v-3h3" /></svg
		>
		<span class="what">Deck {deckId} loop . {loopBarsLabel(deck.loop.beat_length)}</span>
		<span class="tag">drag to an edge</span>
	</button>
{/if}

<style>
	/* Mock `.dragsrc`: loop icon, "Deck N loop . 4 bars", then the "drag to an edge" tag. */
	.loop-drag {
		display: inline-flex;
		align-items: center;
		gap: 8px;
		max-width: 100%;
		padding: 6px 10px;
		border: 1px dashed var(--es-accent);
		border-radius: 5px;
		background: var(--es-drag-src-bg);
		color: var(--es-fg);
		font: 12.5px var(--es-font-body);
		white-space: nowrap;
		cursor: grab;
		touch-action: none;
	}
	.what {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.tag {
		font: 12px var(--es-font-display);
		letter-spacing: 0.7px;
		text-transform: uppercase;
		color: var(--es-dim);
	}
	.loop-drag:active {
		cursor: grabbing;
	}
	.loop-drag svg {
		flex: none;
		width: 15px;
		height: 15px;
		stroke: var(--es-accent);
		fill: none;
		stroke-width: 1.7;
		stroke-linecap: round;
		stroke-linejoin: round;
	}
</style>
