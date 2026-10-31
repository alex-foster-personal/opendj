<script module lang="ts">
	import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
	import type { EventRefresher } from '$lib/rb/grid-edit-queue';

	// Any edit (an agent's, another deck's, or the echo of our own) re-reads
	// the grid once per track, however many decks hold it. The deck supplies
	// the error sink (its toast), the same one for every deck. The queue and
	// its re-read load on the first event or press, off the /performance
	// first paint (bundle budget, PR #6235).
	let sharedEvents: Promise<EventRefresher> | null = null;
	function gridEditEvents(reportError: (message: string) => void): Promise<EventRefresher> {
		sharedEvents ??= import('$lib/rb/grid-edit-api').then((api) => api.gridEditEventRefresher(reportError));
		return sharedEvents;
	}
	function onGridEditEvent(id: string, reportError: (message: string) => void): void {
		gridEditEvents(reportError).then(
			(events) => events.onEvent(id),
			(error: unknown) =>
				reportError(`Grid was edited elsewhere, but this deck could not load its re-read - ${error instanceof Error ? error.message : String(error)}`)
		);
	}
</script>

<script lang="ts">
	// GRIDEDIT-6235: the two left-edge deck buttons (rekordbox's deck grid-edit
	// controls). Top: grid adjust (time nudge). Bottom: grid shift (whole beats).
	// Both drive the same HTTP routes as `opendj track grid-edit`, as the
	// `grid_edit` performance command: the deck's `press` sends it through
	// the shared dispatcher, so a press shares the lifecycle lock and error
	// state with every other deck control.
	import { isUsbTrackId } from '$lib/rb/track-source';
	import type { DeckId, DeckState } from '$lib/rb/deck-state-types';
	import {
		GRID_ADJUST_REPEAT_DELAY_MS,
		GRID_ADJUST_REPEAT_INTERVAL_MS,
		gridHoldTarget,
		startHeldGridNudge,
		gridAdjustTitle,
		gridEditPartLit,
		gridEditUnavailableReason,
		gridShiftTitle,
		intentForKey,
		intentForPress,
		pressOffsetX,
		type GridEditIntent,
		type GridEditPart
	} from '$lib/rb/grid-edit';

	let {
		deck,
		deckId,
		press: sendEdit,
		reportError
	}: {
		deck: DeckState;
		deckId: DeckId;
		/** Dispatches one press as the deck's `grid_edit` performance command. */
		press: (edit: GridEditIntent) => void;
		reportError: (message: string) => void;
	} = $props();


	const stableId = $derived(deck.stable_id);
	const anlz = $derived(deck.anlz);
	const unavailable = $derived(
		gridEditUnavailableReason(stableId, anlz, stableId !== null && isUsbTrackId(stableId))
	);
	const adjustTitle = $derived(gridAdjustTitle(anlz, unavailable));
	const shiftTitle = $derived(gridShiftTitle(anlz, unavailable));
	const offsetMs = $derived(anlz?.beatgrid.edit?.offset_ms ?? 0);
	const beatShift = $derived(anlz?.beatgrid.edit?.beat_shift ?? 0);

	// A resync (first connect, reconnect, seq gap) replays no events, so an
	// agent's edit in that window re-reads the loaded track's grid here.
	$effect(() => {
		const offKind = subscribeKind('beatgrid_edit', (ids) => {
			const id = deck.stable_id;
			if (id !== null && ids.includes(id)) onGridEditEvent(id, reportError);
		});
		const offResync = subscribeResync(() => {
			const id = deck.stable_id;
			if (id !== null) onGridEditEvent(id, reportError);
		});
		return () => {
			offKind();
			offResync();
		};
	});

	function submit(intent: GridEditIntent): void {
		if (unavailable !== null) return;
		// The dispatcher reports a refusal or failure (toast and deck error).
		sendEdit(intent);
	}

	function press(part: GridEditPart, event: MouseEvent): GridEditIntent {
		const target = event.currentTarget as HTMLElement;
		const rect = target.getBoundingClientRect();
		return intentForPress(part, {
			offsetX: pressOffsetX(event.type, event.detail, event.clientX, rect.left),
			width: rect.width,
			shiftKey: event.shiftKey,
			altKey: event.altKey
		});
	}

	// Hold-to-repeat for grid adjust only (a held whole-beat shift would only
	// overshoot: four beats is the same bar 1).
	let stopHold: (() => void) | null = null;
	function stopRepeat(): void {
		stopHold?.();
		stopHold = null;
	}
	$effect(() => stopRepeat);

	function onAdjustPointerDown(event: PointerEvent): void {
		if (event.button !== 0) return;
		const intent = press('adjust', event);
		submit(intent);
		stopRepeat();
		if (intent.kind !== 'nudge') return;
		stopHold = startHeldGridNudge(
			() => submit(intent),
			gridHoldTarget(deck),
			() => gridHoldTarget(deck),
			GRID_ADJUST_REPEAT_DELAY_MS,
			GRID_ADJUST_REPEAT_INTERVAL_MS
		);
	}

	/** A mouse click already acted on pointerdown; a keyboard click (detail 0) acts here. */
	function onAdjustClick(event: MouseEvent): void {
		if (event.detail === 0) submit(press('adjust', event));
	}

	function onShiftClick(event: MouseEvent): void {
		submit(press('shift', event));
	}

	function onKey(part: GridEditPart, event: KeyboardEvent): void {
		const intent = intentForKey(part, event.key, event.shiftKey);
		if (intent === null) return;
		event.preventDefault();
		submit(intent);
	}
</script>

<div class="grid-adjust">
	<button
		class="rb-lit-button"
		class:lit={gridEditPartLit(anlz, 'adjust')}
		disabled={unavailable !== null}
		title={adjustTitle}
		aria-label={`grid adjust deck ${deckId}`}
		data-testid={`grid-adjust-deck-${deckId}`}
		data-rust-command="grid_edit"
		data-offset-ms={offsetMs}
		onpointerdown={onAdjustPointerDown}
		onpointerup={stopRepeat}
		onpointerleave={stopRepeat}
		onpointercancel={stopRepeat}
		onclick={onAdjustClick}
		onkeydown={(event) => onKey('adjust', event)}
	>
		<!-- Three solid grid lines: the grid moving in time. -->
		<svg class="ticks" viewBox="0 0 14 9" aria-hidden="true">
			<path d="M2 0.5V8.5M7 0.5V8.5M12 0.5V8.5" stroke="currentColor" stroke-width="1.6" />
		</svg>
	</button>
	<button
		class="rb-lit-button"
		class:lit={gridEditPartLit(anlz, 'shift')}
		disabled={unavailable !== null}
		title={shiftTitle}
		aria-label={`grid shift deck ${deckId}`}
		data-testid={`grid-shift-deck-${deckId}`}
		data-rust-command="grid_edit"
		data-beat-shift={beatShift}
		onclick={onShiftClick}
		onkeydown={(event) => onKey('shift', event)}
	>
		<!-- Three dashed grid lines: bar positions moving by whole beats. -->
		<svg class="ticks" viewBox="0 0 14 9" aria-hidden="true">
			<path
				d="M2 0.5V8.5M7 0.5V8.5M12 0.5V8.5"
				stroke="currentColor"
				stroke-width="1.6"
				stroke-dasharray="1.6 1.2"
			/>
		</svg>
	</button>
</div>

<style>
	.grid-adjust {
		display: flex;
		flex-direction: column;
		gap: 3px;
		flex: 0 0 auto;
	}
	.grid-adjust button {
		padding: 2px 3px;
		touch-action: none;
	}
	.ticks {
		display: block;
		width: 14px;
		height: 9px;
		fill: none;
		pointer-events: none;
	}
</style>
