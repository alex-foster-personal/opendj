<script lang="ts">
	// Loop cluster (SCREENSHOT-SPEC 3): INT source dropdown (visual-only),
	// big beat-length readout, < > halve/double. Real behaviour via the
	// audio engine: clicking the readout engages/disengages an exact PQTZ
	// beat loop at the current position; halve/double resize it in place.
	// Requires a loaded track with a real beatgrid. The engine also supports
	// interpolated sub-beat loops; this selector offers whole-beat lengths.
	//
	// A very small toggle above the cluster swaps the single big readout for
	// a 4-choice interval grid (< > on top, then a 2x2 of beat lengths).
	// < > shift the window of four choices instead of halving/doubling one
	// value; clicking a choice engages that loop through the same command
	// path as the readout. Readout mode stays the default so existing muscle
	// memory is untouched.
	//
	// Which mode is showing, and which window the grid offers, live in
	// loop-interval-view so the same typed commands drive them from the UI
	// and from an agent - the buttons below dispatch, they do not mutate.
	import type { AnlzBeat } from '$lib/rb/anlz-types';
	import type { DeckId } from '$lib/rb/deck-slots';
	import type { DeckState } from '$lib/rb/deck-state-types';
	import {
		armLoopHover,
		clearLoopHover,
		noteLoopInteraction
	} from '$lib/rb/performance-hotkeys';
	import {
		LOOP_MAX_BEATS,
		LOOP_MIN_BEATS,
		loopIntervalChoices,
		loopIntervalView,
		shiftedLoopIntervalBase
	} from '$lib/rb/loop-interval-view.svelte';
	import {
		canChooseInterval,
		intervalChoiceTip,
		loopDisabledTip,
		loopRestartTip,
		reapplyEngageArgs,
		resizeAnchorOf,
		restartEngageArgs,
		safetyLoopTip
	} from './loop-cluster-actions';
	import { plannedTitle } from '$lib/rb/planned-explainers';
	import LoopSafetyControls from './LoopSafetyControls.svelte';

	let {
		deck,
		deckId,
		pending,
		onEngage,
		onDisengage,
		onSafetySave,
		onSafetyArm,
		onSafetyClear,
		onIntervalMode,
		onIntervalBase
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
		/** Show the interval grid (true) or the single readout (false). */
		onIntervalMode: (enabled: boolean) => Promise<void>;
		/** Move the window of four offered lengths to a new base. */
		onIntervalBase: (base: number) => Promise<void>;
	} = $props();

	const MIN_BEATS = LOOP_MIN_BEATS;
	const MAX_BEATS = LOOP_MAX_BEATS;

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
	/** The engine's own beat_length for the live loop, or null when not
	 * engaged or when the loop was drawn manually rather than through this
	 * grid. Read directly rather than through the local `beatLength` mirror,
	 * which the readout's halve/double still use for their own math and can
	 * hold a stale value unrelated to a manually engaged loop. */
	const engagedIntervalLength: number | null = $derived(
		engaged && deck.loop !== null ? deck.loop.beat_length : null
	);
	const gridBeats: readonly AnlzBeat[] = $derived(deck.anlz?.beatgrid.beats ?? []);
	const gridBeatCount: number = $derived(gridBeats.length);
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

	// --------------------------------------------------- interval grid mode
	// The window is base, 2x, 4x, 8x; < > slide it by one power of two and
	// stay inside the same MIN_BEATS / MAX_BEATS bounds as halve/double.
	const intervalGrid: boolean = $derived(loopIntervalView[deckId].gridMode);
	const gridBase: number = $derived(loopIntervalView[deckId].gridBase);
	const gridChoices: number[] = $derived(loopIntervalChoices(gridBase));
	const prevGridBase: number = $derived(shiftedLoopIntervalBase(gridBase, -1));
	const nextGridBase: number = $derived(shiftedLoopIntervalBase(gridBase, 1));
	// Shifting the window only changes which four lengths are offered, so it
	// stays usable with no track loaded; the choices themselves are what need
	// a real grid. Bounds are the only gate.
	const canGridDown: boolean = $derived(prevGridBase < gridBase);
	const canGridUp: boolean = $derived(nextGridBase > gridBase);
	const disabledTip: string = $derived(loopDisabledTip({ pending, deck, beatLength }));

	// ----------------------------------------------------------- _helpers

	function _fmtBeats(n: number): string {
		return String(n);
	}

	/** A choice is engageable when `n` beats actually fit from the loop
	 * anchor (the engaged loop's in point, or the live position otherwise)
	 * AND end at or before the decoded duration - see canChooseInterval's
	 * own doc comment (loop-cluster-actions.ts) for the full reasoning,
	 * including why the currently engaged length is always choosable
	 * regardless of that fit check. */
	function _canChoose(n: number): boolean {
		return canChooseInterval({ n, pending, deck, engaged, engagedIntervalLength, gridBeats });
	}

	function _choiceTip(n: number): string {
		return intervalChoiceTip({
			n,
			pending,
			deck,
			gridBeatCount,
			engagedIntervalLength,
			canChoose: _canChoose(n)
		});
	}

	async function chooseInterval(n: number): Promise<void> {
		if (!_canChoose(n)) return;
		noteLoopInteraction(deckId);
		// Same command path as the readout: re-picking the live length exits.
		// Reads the engine's own beat_length (engagedIntervalLength), not the
		// local beatLength mirror: a manually drawn loop reports beat_length
		// null, so it must never read as "already at n" and get exited by
		// mistake instead of replaced with an exact n-beat loop.
		if (engagedIntervalLength === n) {
			await onDisengage();
			return;
		}
		// beatLength is NOT set optimistically here: the engine command can
		// still fail (a stale gridBeats read, a race with track unload), and
		// runPerformanceCommandFromUi swallows that rejection, so an
		// optimistic write would leave the readout showing a length the
		// engine never actually engaged. The $effect above syncs beatLength
		// from deck.loop once the engine publishes the real state.
		if (engaged && deck.loop !== null) await onEngage(n, deck.loop.in_ms);
		else await onEngage(n);
	}

	async function toggleIntervalMode(): Promise<void> {
		noteLoopInteraction(deckId);
		await onIntervalMode(!intervalGrid);
	}

	async function shiftGrid(direction: -1 | 1): Promise<void> {
		const next = direction < 0 ? prevGridBase : nextGridBase;
		if (next === gridBase) return;
		noteLoopInteraction(deckId);
		await onIntervalBase(next);
	}

	/** Which side of the loop stays fixed while resizing - see
	 * resizeAnchorOf's own doc comment (loop-cluster-actions.ts) for the
	 * click-modifier mapping. */
	function _resizeAnchor(event: MouseEvent): 'start' | 'end' | 'center' {
		return resizeAnchorOf(event);
	}

	async function _reapply(anchor: 'start' | 'end' | 'center' = 'start'): Promise<void> {
		if (!engaged) return;
		const args = reapplyEngageArgs({ anchor, deck, gridBeats, beatLength });
		if (args === null) return;
		await onEngage(args.beats, args.startMs);
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

	async function halve(event: MouseEvent): Promise<void> {
		if (!canHalve) return;
		noteLoopInteraction(deckId);
		const anchor = _resizeAnchor(event);
		beatLength = nextHalved;
		await _reapply(anchor);
	}

	async function double(event: MouseEvent): Promise<void> {
		if (!canDouble) return;
		noteLoopInteraction(deckId);
		const anchor = _resizeAnchor(event);
		beatLength = nextDoubled;
		await _reapply(anchor);
	}

	/** Reissue the live loop's own beat length + in point unchanged - see
	 * restartEngageArgs' own doc comment (loop-cluster-actions.ts) for why
	 * the engine treats the exact match as a restart. */
	async function restartLoop(): Promise<void> {
		if (pending || !engaged) return;
		const args = restartEngageArgs(deck);
		if (args === null) return;
		noteLoopInteraction(deckId);
		await onEngage(args.beats, args.startMs);
	}

	const canRestart: boolean = $derived(
		!pending && engaged && deck.loop !== null && deck.loop.beat_length !== null
	);
	const restartTip: string = $derived(loopRestartTip({ pending, engaged, canRestart }));

	// ------------------------------------------------------ safety loop

	const safety = $derived(deck.safety_loop);
	/** The engine throws unless a loop is actually engaged, so gate the
	 * button on the same condition rather than letting it error. */
	const canSaveSafety: boolean = $derived(!pending && engaged);
	const safetyTip: string = $derived(safetyLoopTip({ pending, canSaveSafety }));
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
	class="loop-cluster"
	role="group"
	aria-label={`loop controls deck ${deckId}`}
	onpointerenter={() => armLoopHover(deckId)}
	onpointerleave={() => clearLoopHover(deckId)}
>
	<span class="column-label">LOOP</span>
	<button
		class="mode-toggle"
		class:on={intervalGrid}
		data-performance-control="loop-interval-mode"
		data-testid={`loop-interval-mode-deck-${deckId}`}
		aria-label={`loop interval mode deck ${deckId}`}
		data-state={intervalGrid ? 'on' : 'off'}
		aria-pressed={intervalGrid}
		title={intervalGrid
			? 'switch back to the single beat-length readout'
			: 'switch to the 4-choice loop interval grid'}
		onclick={() => void toggleIntervalMode()}
	>
		<span class="mode-glyph" aria-hidden="true">&#9647;&#9647;</span>
	</button>
	<button class="rb-lit-button rb-inert int" disabled title={plannedTitle('loop-source')} aria-label={`loop source deck ${deckId}`} data-testid={`loop-source-deck-${deckId}`}>
		INT <span class="caret">&#9662;</span>
	</button>
	{#if intervalGrid}
		<div class="halve-double">
			<button
				disabled={!canGridDown}
				data-performance-control="loop-interval-down"
				data-testid={`loop-interval-down-deck-${deckId}`}
				aria-label={`smaller loop intervals deck ${deckId}`}
				title={canGridDown
					? `show smaller loop intervals (${prevGridBase} to ${prevGridBase * 8} beats)`
					: `already at the smallest interval window (${MIN_BEATS} beats)`}
				onclick={() => void shiftGrid(-1)}
			>
				&lt;
			</button>
			<button
				disabled={!canGridUp}
				data-performance-control="loop-interval-up"
				data-testid={`loop-interval-up-deck-${deckId}`}
				aria-label={`larger loop intervals deck ${deckId}`}
				title={canGridUp
					? `show larger loop intervals (${nextGridBase} to ${nextGridBase * 8} beats)`
					: `already at the largest interval window (${MAX_BEATS} beats)`}
				onclick={() => void shiftGrid(1)}
			>
				&gt;
			</button>
		</div>
		<div
			class="interval-grid"
			title="Loop interval choices in beats - click one to loop that many beatgrid beats"
		>
			{#each gridChoices as choice (choice)}
				<button
					class:selected={engagedIntervalLength === choice}
					disabled={!_canChoose(choice)}
					data-performance-control="loop-interval"
					data-testid={`loop-${choice}-beats-deck-${deckId}`}
					aria-label={`loop ${choice} beats deck ${deckId}`}
					aria-pressed={engagedIntervalLength === choice}
					data-beats={choice}
					data-state={engagedIntervalLength === choice ? 'on' : 'off'}
					title={_choiceTip(choice)}
					onclick={() => void chooseInterval(choice)}
				>
					{_fmtBeats(choice)}
				</button>
			{/each}
		</div>
	{:else}
		<div class="readout-wrap">
			<button
				class="readout"
				class:engaged
				disabled={!canToggle}
				data-performance-control="loop"
				data-testid={`loop-deck-${deckId}`}
				aria-label={`loop deck ${deckId}`}
				aria-pressed={engaged}
				data-state={engaged ? 'on' : 'off'}
				title={canToggle ? (engaged ? 'exit loop' : `loop ${_fmtBeats(beatLength)} beats`) : disabledTip}
				onclick={toggleLoop}
			>
				{_fmtBeats(beatLength)}
			</button>
			<button
				class="restart-btn"
				disabled={!canRestart}
				data-performance-control="loop-restart"
				aria-label="restart loop"
				title={restartTip}
				onclick={() => void restartLoop()}
			>
				<svg viewBox="0 0 16 16" width="9" height="9" aria-hidden="true" focusable="false">
					<path
						d="M8 2.5a5.5 5.5 0 1 1-5.196 3.7"
						fill="none"
						stroke="currentColor"
						stroke-width="1.6"
						stroke-linecap="round"
					/>
					<path
						d="M1.6 3.4v3.1h3.1"
						fill="none"
						stroke="currentColor"
						stroke-width="1.6"
						stroke-linecap="round"
						stroke-linejoin="round"
					/>
				</svg>
			</button>
		</div>
		<div class="halve-double">
			<button
				disabled={!canHalve}
				data-performance-control="loop-halve"
				data-testid={`loop-halve-deck-${deckId}`}
				aria-label={`halve loop deck ${deckId}`}
				title={canHalve
					? 'halve loop length - shift: anchor loop end, opt: anchor center'
					: disabledTip}
				onclick={(event) => void halve(event)}
			>
				&lt;
			</button>
			<button
				disabled={!canDouble}
				data-performance-control="loop-double"
				data-testid={`loop-double-deck-${deckId}`}
				aria-label={`double loop deck ${deckId}`}
				title={canDouble
					? 'double loop length - shift: anchor loop end, opt: anchor center'
					: disabledTip}
				onclick={(event) => void double(event)}
			>
				&gt;
			</button>
		</div>
	{/if}
	<LoopSafetyControls
		{deckId}
		{pending}
		{safety}
		{canSaveSafety}
		{safetyTip}
		{onSafetySave}
		{onSafetyArm}
		{onSafetyClear}
	/>
</div>

<style>
	.loop-cluster {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 3px;
		flex: 0 0 auto;
		/* Matches the readout width so switching to the interval grid cannot
		 * narrow the column and reflow the rest of the deck row. Also capped
		 * at max-width so it does not stretch to match BeatJump, its wider
		 * sibling in the shared loop-col (Deck.svelte), under loop-col's
		 * align-items: stretch. */
		min-width: 44px;
		max-width: 44px;
	}
	.column-label {
		color: var(--rb-text-dim);
		font-size: 7px;
		font-weight: 700;
		line-height: 7px;
		letter-spacing: 0.45px;
	}
	/* Deliberately tiny: a mode switch, not a feature button. */
	.mode-toggle {
		width: 100%;
		height: 7px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		padding: 0;
		cursor: pointer;
		opacity: 0.65;
	}
	.mode-toggle:hover {
		opacity: 1;
		border-color: var(--rb-accent);
	}
	.mode-toggle.on {
		opacity: 1;
		border-color: var(--rb-orange);
	}
	.mode-glyph {
		font-size: 6px;
		line-height: 1;
		letter-spacing: 1px;
		color: var(--rb-text-dim);
	}
	.mode-toggle.on .mode-glyph {
		color: var(--rb-orange);
	}
	.int {
		width: 100%;
		text-align: center;
	}
	.interval-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 2px;
		width: 100%;
	}
	.interval-grid button {
		background: #080a0d;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: 10px;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		line-height: 1;
		padding: 1px 0;
		cursor: pointer;
	}
	.interval-grid button:hover:not(:disabled) {
		border-color: var(--rb-accent);
		color: var(--rb-accent);
	}
	.interval-grid button.selected {
		color: var(--rb-orange);
		border-color: var(--rb-orange);
		box-shadow: 0 0 5px rgba(232, 161, 58, 0.5);
	}
	.interval-grid button:disabled {
		color: var(--rb-text-dim);
		cursor: default;
		opacity: 0.6;
	}
	.caret {
		color: var(--rb-text-dim);
	}
	.readout-wrap {
		position: relative;
		width: 44px;
	}
	.restart-btn {
		position: absolute;
		top: 1px;
		right: 1px;
		width: 12px;
		height: 12px;
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 0;
		background: rgba(8, 10, 13, 0.75);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		cursor: pointer;
	}
	.restart-btn:hover:not(:disabled) {
		color: var(--rb-accent);
		border-color: var(--rb-accent);
	}
	.restart-btn:disabled {
		opacity: 0.35;
		cursor: default;
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
</style>
