<script lang="ts">
	// DECKCTL-6235d (alpha status I14): the loop cluster's INT source selector.
	// INT = whole-beat loops from the beatgrid (the readout and interval grid
	// beside it). The menu also lists the track's stored loops (hot-cue loops
	// and memory loops); picking one engages exactly that range through the
	// `loop` command an agent sends too. The button names the source of the
	// loop in force. Placement mirrors PadModeMenu: fixed, from the trigger.
	import type { DeckId, DeckState } from '$lib/rb/deck-state-types';
	import {
		engagedStoredLoop,
		loopStateKey,
		pickedAfter,
		pickedForLoad,
		pickedWhileInForce,
		storedLoops,
		type StoredLoop
	} from '$lib/rb/loop-source';
	import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';

	let {
		deck,
		deckId,
		pending,
		onStoredLoop,
		onDisengage
	}: {
		deck: DeckState;
		deckId: DeckId;
		pending: boolean;
		/** Engage this exact stored range; true when the engine took it. */
		onStoredLoop: (loop: { in_ms: number; out_ms: number }) => Promise<boolean>;
		/** Drop the engaged loop (INT picked while a stored loop is in force);
		 * true when the engine took it. */
		onDisengage: () => Promise<boolean>;
	} = $props();

	let open = $state(false);
	/** The row last picked, so a range two stored loops share names the right
	 * one; it belongs to the load it was picked on, never a later track, and
	 * ends when the loop it named stops being in force. */
	let picked: { load: number; id: string | null; seen: boolean; since: string } = $state({ load: -1, id: null, seen: false, since: 'none' });
	const pickedId: string | null = $derived(pickedForLoad(picked, deck.load_generation));
	let wrapEl: HTMLSpanElement | undefined = $state();
	let triggerEl: HTMLButtonElement | undefined = $state();

	const loops: StoredLoop[] = $derived(storedLoops(deck.anlz?.cues));
	const inForce: StoredLoop | null = $derived(engagedStoredLoop(loops, deck.loop, pickedId));
	const label: string = $derived(inForce === null ? 'INT' : inForce.short);
	$effect(() => {
		const next = pickedWhileInForce(picked, inForce?.id ?? null, loopStateKey(deck.loop));
		if (next !== picked) picked = next;
	});
	const loaded: boolean = $derived(deck.stable_id !== null);
	const title: string = $derived(
		!loaded
			? 'Loop source - load a track to pick where loops come from'
			: inForce !== null
				? `Loop source: ${inForce.label}, engaged. INT loops come from the beatgrid; open to pick another stored loop.`
				: loops.length === 0
					? 'Loop source: INT - loops are whole beats from the beatgrid. This track has no stored hot-cue or memory loops.'
					: `Loop source: INT - loops are whole beats from the beatgrid. Open to engage one of this track's ${loops.length} stored loops.`
	);

	function onWindowPointerDown(e: PointerEvent): void {
		if (!open) return;
		const target = e.target;
		if (target instanceof Node && wrapEl?.contains(target)) return;
		open = false;
	}

	function onWindowKeyDown(e: KeyboardEvent): void {
		if (!open || e.key !== 'Escape') return;
		open = false;
		triggerEl?.focus();
	}

	// The picked row changes only once the engine took the command, so a
	// refused one (a preset owns the controls) keeps naming the loop in force.
	async function pickInt(): Promise<void> {
		open = false;
		const load = deck.load_generation;
		// Sampled before the command goes out, so any loop state after it counts.
		const since = loopStateKey(deck.loop);
		const id = inForce === null ? null : await pickedAfter(onDisengage, pickedId, null);
		picked = { load, id, seen: false, since };
	}

	async function pick(loop: StoredLoop): Promise<void> {
		open = false;
		const load = deck.load_generation;
		const since = loopStateKey(deck.loop);
		const id = await pickedAfter(() => onStoredLoop({ in_ms: loop.in_ms, out_ms: loop.out_ms }), pickedId, loop.id);
		picked = { load, id, seen: false, since };
	}
</script>

<svelte:window onpointerdown={onWindowPointerDown} onkeydown={onWindowKeyDown} />

<span class="source-wrap" bind:this={wrapEl}>
	<button
		bind:this={triggerEl}
		class="rb-lit-button int"
		class:lit={inForce !== null}
		disabled={!loaded}
		title={title}
		aria-label={`loop source deck ${deckId}`}
		aria-haspopup="menu"
		aria-expanded={open}
		data-testid={`loop-source-deck-${deckId}`}
		data-source={inForce === null ? 'int' : inForce.source}
		onclick={() => (open = !open)}
	>
		{label}
	</button>
	{#if open}
		<div
			class="source-menu"
			role="menu"
			aria-label={`loop source menu deck ${deckId}`}
			use:triggerFloatingAction={{ getTrigger: () => triggerEl ?? null, preferred: 'below', gap: 4 }}
		>
			<button
				type="button"
				class="source-item"
				class:selected={inForce === null}
				role="menuitemradio"
				aria-checked={inForce === null}
				data-testid={`loop-source-int-deck-${deckId}`}
				title={inForce === null
					? 'INT - the loop readout and interval grid engage whole-beat loops from the beatgrid at the playhead'
					: 'INT - exit the stored loop; the loop readout and interval grid engage whole-beat loops from the beatgrid'}
				onclick={() => void pickInt()}
			>
				INT - beat loops from the grid
			</button>
			{#each loops as loop (loop.id)}
				<button
					type="button"
					class="source-item"
					class:selected={inForce?.id === loop.id}
					role="menuitemradio"
					aria-checked={inForce?.id === loop.id}
					disabled={pending}
					data-testid={`loop-source-${loop.id}-deck-${deckId}`}
					title={`Engage this stored loop: ${loop.label}`}
					onclick={() => void pick(loop)}
				>
					{loop.label}
				</button>
			{:else}
				<div class="source-empty" role="presentation">No stored loops on this track</div>
			{/each}
		</div>
	{/if}
</span>

<style>
	.source-wrap {
		position: relative;
		display: block;
		width: 100%;
	}
	.int {
		width: 100%;
		text-align: center;
	}
	/* CHROME-01: the caret is a CSS border triangle, never a text glyph. */
	.int::after {
		content: '';
		display: inline-block;
		width: 0;
		height: 0;
		margin-left: 4px;
		vertical-align: middle;
		border-left: 3px solid transparent;
		border-right: 3px solid transparent;
		border-top: 4px solid var(--rb-text-dim);
	}
	.source-menu {
		position: fixed;
		z-index: 1000;
		min-width: 220px;
		background: var(--rb-panel);
		border: 1px solid var(--rb-border);
		border-radius: 4px;
		padding: 4px 0;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.45);
	}
	.source-item {
		display: block;
		width: 100%;
		text-align: left;
		background: transparent;
		border: none;
		color: var(--rb-text);
		font-size: 11px;
		padding: 5px 10px;
		cursor: pointer;
	}
	.source-item:hover:not(:disabled) {
		background: rgba(255, 255, 255, 0.06);
	}
	.source-item.selected {
		color: var(--rb-accent);
	}
	.source-empty {
		color: var(--rb-text-dim);
		font-size: 11px;
		padding: 5px 10px;
	}
</style>
