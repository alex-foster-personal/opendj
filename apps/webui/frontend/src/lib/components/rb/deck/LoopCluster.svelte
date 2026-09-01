<script lang="ts">
	// Loop cluster (SCREENSHOT-SPEC 3): INT source dropdown (visual-only),
	// big beat-length readout, < > halve/double. Real behaviour via the
	// audio engine: clicking the readout engages/disengages an exact PQTZ
	// beat loop at the current position; halve/double resize it in place.
	// Requires a loaded track with a real beatgrid. Sub-beat loops remain
	// unimplemented until the engine has honest PQTZ interpolation.
	import type { DeckId } from '$lib/rb/deck-slots';
	import type { DeckState } from '$lib/rb/deck-state-types';
	import {
		armLoopHover,
		clearLoopHover,
		noteLoopInteraction
	} from '$lib/rb/performance-hotkeys';

	let {
		deck,
		deckId,
		pending,
		onEngage,
		onDisengage,
		onSafetySave,
		onSafetyArm,
		onSafetyClear,
		inertTip
	}: {
		deck: DeckState;
		deckId: DeckId;
		pending: boolean;
		onEngage: (beats: number, startMs?: number) => Promise<void>;
		onDisengage: () => Promise<void>;
		/** Capture the engaged loop into the one safety slot (arms it). */
		onSafetySave: () => Promise<void>;
		/** Arm or disarm the saved safety loop. */
		onSafetyArm: (armed: boolean) => Promise<void>;
		/** Drop the saved safety loop entirely. */
		onSafetyClear: () => Promise<void>;
		inertTip: string;
	} = $props();

	const MIN_BEATS = 1;
	const MAX_BEATS = 512;

	let beatLength: number = $state(4);

	$effect(() => {
		if (
			deck.loop !== null &&
			deck.loop.beat_length !== null &&
			beatLength !== deck.loop.beat_length
		) {
			beatLength = deck.loop.beat_length;
		}
	});

	const engaged: boolean = $derived(deck.loop !== null && deck.loop.engaged);
	const gridBeatCount: number = $derived(deck.anlz?.beatgrid.beats.length ?? 0);
	const canToggle: boolean = $derived(
		!pending && deck.stable_id !== null && (engaged || gridBeatCount > beatLength)
	);
	const nextHalved: number = $derived(Math.max(MIN_BEATS, Math.floor(beatLength / 2)));
	const nextDoubled: number = $derived(Math.min(MAX_BEATS, beatLength * 2));
	const canHalve: boolean = $derived(
		!pending && deck.stable_id !== null && gridBeatCount > nextHalved
	);
	const canDouble: boolean = $derived(
		!pending &&
		deck.stable_id !== null &&
		nextDoubled > beatLength &&
		gridBeatCount > nextDoubled
	);
	const disabledTip: string = $derived(
		pending
			? 'deck command pending'
			: deck.stable_id === null
			? 'no track loaded'
			: deck.anlz === null || deck.anlz.beatgrid.beats.length === 0
				? 'track has no beatgrid - beat loop unavailable'
				: deck.anlz.beatgrid.beats.length <= beatLength
					? `beatgrid has too few beats for a ${beatLength}-beat loop`
				: ''
	);

	// ----------------------------------------------------------- _helpers

	function _fmtBeats(n: number): string {
		return String(n);
	}

	async function _reapply(): Promise<void> {
		// Resize a live loop keeping its in point.
		if (!engaged || deck.loop === null) return;
		await onEngage(beatLength, deck.loop.in_ms);
	}

	async function toggleLoop(): Promise<void> {
		if (!canToggle) return;
		noteLoopInteraction(deckId);
		if (engaged) {
			await onDisengage();
		} else {
			await onEngage(beatLength);
		}
	}

	async function halve(): Promise<void> {
		if (!canHalve) return;
		noteLoopInteraction(deckId);
		beatLength = nextHalved;
		await _reapply();
	}

	async function double(): Promise<void> {
		if (!canDouble) return;
		noteLoopInteraction(deckId);
		beatLength = nextDoubled;
		await _reapply();
	}

	// ------------------------------------------------------ safety loop

	const safety = $derived(deck.safety_loop);
	/** The engine throws unless a loop is actually engaged, so gate the
	 * button on the same condition rather than letting it error. */
	const canSaveSafety: boolean = $derived(!pending && engaged);
	const safetyTip: string = $derived(
		canSaveSafety
			? 'save the engaged loop as this deck armed safety loop'
			: pending
				? 'deck command pending'
				: 'engage a loop first - there is nothing to save'
	);

	async function saveSafety(): Promise<void> {
		if (!canSaveSafety) return;
		noteLoopInteraction(deckId);
		await onSafetySave();
	}

	async function toggleSafetyArmed(): Promise<void> {
		if (pending || safety === null) return;
		noteLoopInteraction(deckId);
		await onSafetyArm(!safety.armed);
	}

	async function clearSafety(): Promise<void> {
		if (pending || safety === null) return;
		noteLoopInteraction(deckId);
		await onSafetyClear();
	}
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
	class="loop-cluster"
	onpointerenter={() => armLoopHover(deckId)}
	onpointerleave={() => clearLoopHover(deckId)}
>
	<button class="rb-lit-button rb-inert int" disabled title={inertTip}>
		INT <span class="caret">&#9662;</span>
	</button>
	<button
		class="readout"
		class:engaged
		disabled={!canToggle}
		data-performance-control="loop"
		data-state={engaged ? 'on' : 'off'}
		title={canToggle ? (engaged ? 'exit loop' : `loop ${_fmtBeats(beatLength)} beats`) : disabledTip}
		onclick={toggleLoop}
	>
		{_fmtBeats(beatLength)}
	</button>
	<div class="halve-double">
		<button
			disabled={!canHalve}
			data-performance-control="loop-halve"
			title={canHalve ? 'halve loop length' : disabledTip}
			onclick={halve}
		>
			&lt;
		</button>
		<button
			disabled={!canDouble}
			data-performance-control="loop-double"
			title={canDouble ? 'double loop length' : disabledTip}
			onclick={double}
		>
			&gt;
		</button>
	</div>
	<div class="safety">
		{#if safety === null}
			<button
				class="safety-btn"
				disabled={!canSaveSafety}
				data-performance-control="safety-loop-save"
				title={safetyTip}
				onclick={saveSafety}
			>
				SAFE
			</button>
		{:else}
			<button
				class="safety-btn"
				class:armed={safety.armed}
				disabled={pending}
				data-performance-control="safety-loop-arm"
				data-state={safety.armed ? 'on' : 'off'}
				aria-pressed={safety.armed}
				title={`safety loop ${safety.beat_length ?? '?'} beats at ${Math.round(safety.in_ms)} ms - ${safety.armed ? 'armed, engages instead of running off the end' : 'saved but disarmed'}`}
				onclick={toggleSafetyArmed}
			>
				SAFE {safety.beat_length ?? ''}
			</button>
			<button
				class="safety-clear"
				disabled={pending}
				data-performance-control="safety-loop-clear"
				title="clear the saved safety loop"
				aria-label="clear safety loop"
				onclick={clearSafety}
			>
				&times;
			</button>
		{/if}
	</div>
</div>

<style>
	.loop-cluster {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 3px;
		flex: 0 0 auto;
	}
	.int {
		width: 100%;
		text-align: center;
	}
	.caret {
		color: var(--rb-text-dim);
	}
	.readout {
		width: 44px;
		background: #080a0d;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: 20px;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		text-align: center;
		padding: 2px 0;
		cursor: pointer;
	}
	.readout.engaged {
		color: var(--rb-orange);
		border-color: var(--rb-orange);
		box-shadow: 0 0 5px rgba(232, 161, 58, 0.5);
	}
	.readout:disabled {
		color: var(--rb-text-dim);
		cursor: default;
		opacity: 0.6;
	}
	.halve-double {
		display: flex;
		gap: 2px;
		width: 100%;
	}
	.halve-double button {
		flex: 1;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		padding: 1px 0;
		cursor: pointer;
	}
	.halve-double button:disabled {
		color: var(--rb-text-dim);
		cursor: default;
		opacity: 0.6;
	}
	.safety {
		display: flex;
		gap: 2px;
		width: 100%;
	}
	.safety button {
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		padding: 1px 0;
		cursor: pointer;
	}
	.safety-btn {
		flex: 1;
		font-variant-numeric: tabular-nums;
	}
	.safety-btn.armed {
		border-color: var(--rb-orange);
		color: var(--rb-orange);
	}
	.safety-clear {
		flex: 0 0 16px;
	}
	.safety button:disabled {
		color: var(--rb-text-dim);
		cursor: default;
		opacity: 0.6;
	}
</style>
