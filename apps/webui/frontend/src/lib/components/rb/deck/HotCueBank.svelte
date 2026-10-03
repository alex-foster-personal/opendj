<script lang="ts">
	import { tick } from 'svelte';
	// 8-slot hot-cue bank A-H as WIDE slot rows, 2 columns x 4 rows (A-D
	// left, E-H right, SCREENSHOT-SPEC 3 wide layout): the informative rows
	// fill spare width for cue labels while their height stays bounded so
	// they do not consume the deck's spare vertical space. Populated
	// slot click = real jump to in_ms via the audio engine (COMPONENT-MAP
	// 1.3). Empty slot click = SAVE the current playhead there (djmdCue
	// Kind 1-8 write path); the x on a filled slot clears it. Kind 9-11
	// (beyond H) is unverified and never exposed (PARITY-TODO.md). HOT CUE
	// dropdown selector below-left is visual-only (inert).
	//
	// Cues live in Open DJ's own cue store (CUES-01), so every loaded track
	// can SAVE, rekordbox-mapped or not; the #736 mapping gate is gone.
	//
	// An empty deck (deck.stable_id null - nothing loaded, or momentarily
	// mid-reload) has no track to save onto, so its slots go
	// inert-with-tooltip, gated on stable_id (issue #804).
	import { fetchTrackLyrics, type HotCueMutation } from '$lib/rb/api-rb';
	import type { DeckState } from '$lib/rb/deck-state-types';
	import type { HotCue, HotCueSlot } from '$lib/rb/hot-cue-types';
	import { hotCueTitle } from '$lib/rb/hot-cue-label';
	import { plannedTitle } from '$lib/rb/planned-explainers';
	import { proposalTitle, visibleProposalForSlot } from '$lib/rb/auto-cue-proposals';
	import { ensureAutoCues, getAutoCuesEntry } from './auto-cues-cache.svelte';
	import HotCueProposalLabel from './HotCueProposalLabel.svelte';
	import { createLyricsFetchState } from '../wave/lyrics-fetch.svelte';

	const NOT_LOADED_TIP = 'no track loaded - nothing to save';

	let {
		deck,
		pending,
		onJump,
		onSave,
		onRename,
		onDelete,
		onRestore
	}: {
		deck: DeckState;
		pending: boolean;
		/** #884: slot-addressed, not a raw ms - lets the dispatcher honour
		 * BeatSyncMax (arm for the deck's own next downbeat) instead of a plain
		 * unconditional seek. pressT0Ms is the triggering click's own
		 * event.timeStamp, Q1's operator-felt press stamp. */
		onJump: (slot: HotCueSlot, pressT0Ms?: number) => Promise<void>;
		onSave: (slot: HotCueSlot, comment?: string, fixedPositionMs?: number, quantizeFixedPosition?: boolean, expectedStableId?: string) => Promise<HotCueMutation>;
		onRename: (slot: HotCueSlot, inMs: number, comment: string) => Promise<HotCueMutation>;
		onDelete: (slot: HotCueSlot) => Promise<HotCueMutation>;
		onRestore: (slot: HotCueSlot, revision: string, reversalId: string) => Promise<void>;
	} = $props();

	const SLOTS: HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

	const bank: { slot: HotCueSlot; cue: HotCue | null }[] = $derived(
		SLOTS.map((slot) => ({
			slot,
			cue: deck.hot_cues.find((c) => c.slot === slot) ?? null
		}))
	);

	$effect(() => {
		const sid = deck.stable_id;
		if (sid !== null) ensureAutoCues(sid);
	});
	const lyricsState = createLyricsFetchState(() => deck.stable_id, fetchTrackLyrics);
	const filledSlots = $derived(new Set(deck.hot_cues.map((c) => c.slot)));
	const proposalFor = $derived.by(() => {
		const sid = deck.stable_id;
		if (sid === null) return null;
		const entry = getAutoCuesEntry(sid);
		if (entry === undefined || entry.status !== 'ready') return null;
		return entry.data.proposals;
	});

	// Local write-round-trip busy state, separate from `pending` (transport
	// commands) so a save/clear in flight only disables its own slot. `pending`
	// itself no longer disables anything: greying every pad out for the duration
	// of an unrelated transport command is the same one-frame blink the play
	// button had (LATENCY-01 visual feedback). It stays visible as aria-busy.
	let busySlot: HotCueSlot | null = $state(null);
	let busySlotCompletion: Promise<void> = Promise.resolve();
	let undo: { slot: HotCueSlot; revision: string; reversalId: string } | null = $state(null);
	let renameSlot: HotCueSlot | null = $state(null);
	let renameNewCueAtMs: number | null = $state(null);
	let renameStableId: string | null = $state(null);
	let renameDraft = $state('');
	let renameInputEl: HTMLInputElement | null = $state(null);
	// DECKUX-03 greedy columns: which column (1 = A-D, 2 = E-H) currently
	// has hover or focus. null = neither, both columns sit at 50/50. Tracked
	// SEPARATELY (bot review P3): hover and focus used to share one variable,
	// so focusing a pad then hovering into and back out of its column let
	// mouseleave clear it while focus was still inside. activeCol stays set
	// while EITHER interaction is live.
	let hoverCol: 1 | 2 | null = $state(null);
	let focusCol: 1 | 2 | null = $state(null);
	const activeCol = $derived(hoverCol ?? focusCol);

	async function acquireBusySlot(slot: HotCueSlot): Promise<() => void> {
		const previousCompletion = busySlotCompletion;
		let release!: () => void;
		busySlotCompletion = new Promise<void>((resolve) => {
			release = resolve;
		});
		await previousCompletion;
		busySlot = slot;
		return () => {
			busySlot = null;
			release();
		};
	}

	async function onSlotClick(
		entry: { slot: HotCueSlot; cue: HotCue | null },
		pressT0Ms?: number
	): Promise<void> {
		if (entry.cue !== null) {
			await onJump(entry.slot, pressT0Ms);
			return;
		}
		// Empty deck rows are inert (WaveRow.svelte precedent): stable_id is
		// the "is there a deck to save onto" signal (#804).
		if (deck.stable_id === null) return;
		await beginRename(entry.slot, deck.position_ms, deck.stable_id);
	}

	async function beginRename(
		slot: HotCueSlot,
		newCueAtMs: number | null,
		stableId: string,
		draft = ''
	): Promise<void> {
		renameSlot = slot;
		renameNewCueAtMs = newCueAtMs;
		renameStableId = stableId;
		renameDraft = draft;
		await tick();
		if (renameInputEl === null) throw new Error(`hot cue ${slot}: name input did not mount`);
		renameInputEl.focus();
		renameInputEl.select();
	}

	// DECKUX-02 (pin c20eeb07cae0): reopen an ALREADY-named cue for editing.
	// Goes through the same beginRename/commitRename flow as creation - the
	// only difference is newCueAtMs stays null, so commitRename takes the
	// existing-cue onRename branch instead of a new onSave. stopPropagation
	// keeps this off the button's own onSlotClick (which would jump instead).
	async function onEditClick(
		entry: { slot: HotCueSlot; cue: HotCue | null },
		event: MouseEvent | KeyboardEvent
	): Promise<void> {
		event.stopPropagation();
		if (entry.cue === null || deck.stable_id === null) return;
		await beginRename(entry.slot, null, deck.stable_id, entry.cue.comment ?? '');
	}

	async function commitRename(entry: { slot: HotCueSlot; cue: HotCue | null }): Promise<void> {
		if (renameSlot !== entry.slot) return;
		const comment = renameDraft.trim() === '' ? undefined : renameDraft;
		const newCueAtMs = renameNewCueAtMs;
		const stableId = renameStableId;
		const release = await acquireBusySlot(entry.slot);
		try {
			if (newCueAtMs !== null) {
				if (stableId === null) throw new Error(`hot cue ${entry.slot}: rename track is unavailable`);
				setUndo(entry.slot, await onSave(entry.slot, comment, newCueAtMs, true, stableId));
			} else if (entry.cue !== null) {
				setUndo(entry.slot, await onRename(entry.slot, entry.cue.in_ms, comment ?? ''));
			} else {
				throw new Error(`hot cue ${entry.slot}: rename target is unavailable`);
			}
			if (renameSlot === entry.slot) {
				renameSlot = null;
				renameNewCueAtMs = null;
				renameStableId = null;
				renameInputEl = null;
			}
		} finally {
			release();
		}
	}

	function cancelRename(slot: HotCueSlot): void {
		if (renameSlot !== slot) return;
		renameSlot = null;
		renameNewCueAtMs = null;
		renameStableId = null;
		renameInputEl = null;
	}

	async function onClearClick(slot: HotCueSlot, event: MouseEvent): Promise<void> {
		event.stopPropagation();
		// Not reachable today (the x only renders for entry.cue !== null, and an
		// empty deck's hot_cues is always []), closed defensively while this
		// guard is already under review (#804 audit comment).
		if (deck.stable_id === null) return;
		const release = await acquireBusySlot(slot);
		try {
			setUndo(slot, await onDelete(slot));
		} finally {
			release();
		}
	}

	function setUndo(slot: HotCueSlot, mutation: HotCueMutation): void {
		if (mutation.reversal === undefined) {
			throw new Error(`hot cue ${slot}: server omitted reversal token`);
		}
		undo = { slot, revision: mutation.revision, reversalId: mutation.reversal.reversal_id };
	}

	async function undoLastMutation(): Promise<void> {
		const requestedSlot = undo?.slot;
		if (requestedSlot === undefined) return;
		const release = await acquireBusySlot(requestedSlot);
		try {
			if (undo === null) return;
			const target = undo;
			await onRestore(target.slot, target.revision, target.reversalId);
			undo = null;
		} finally {
			release();
		}
	}
</script>

<div class="cue-area" role="group" aria-label={`hot cues deck ${deck.deck_id}`}>
	<div class="bank" role="group" aria-label={`hot cue pads deck ${deck.deck_id}`}>
		<!-- DECKUX-03 (pin c20eeb07cae0): the two hot-cue columns (A-D, E-H)
		     are greedy - hovering OR focusing inside a column grows it to
		     ~80% of the bank's width so long labels are readable, the other
		     column shrinking to make room. onfocusin/onfocusout (not
		     focus/blur) bubble from the pads and the rename input up to
		     this wrapper, so keyboard focus is an equal path to hover, not
		     a mouse-only affordance. -->
		{#each [bank.slice(0, 4), bank.slice(4, 8)] as column, columnIndex (columnIndex)}
			{@const col = (columnIndex + 1) as 1 | 2}
			<div
				class="cue-col"
				role="group"
				aria-label={`hot cue column ${columnIndex + 1} deck ${deck.deck_id}`}
				class:grow={activeCol === col}
				class:shrink={activeCol !== null && activeCol !== col}
				onmouseenter={() => (hoverCol = col)}
				onmouseleave={() => {
					if (hoverCol === col) hoverCol = null;
				}}
				onfocusin={() => (focusCol = col)}
				onfocusout={() => {
					if (focusCol === col) focusCol = null;
				}}
			>
				{#each column as entry (entry.slot)}
					{@const visible = proposalFor === null ? null : visibleProposalForSlot(entry.slot, filledSlots, proposalFor)}
					<div class="slot-cell">
						<button
							class="slot"
							class:filled={entry.cue !== null}
							class:proposal={entry.cue === null && visible !== null}
							class:loop={entry.cue !== null && entry.cue.is_loop}
							class:inert-mapping={entry.cue === null && deck.stable_id === null}
							disabled={busySlot === entry.slot || renameSlot === entry.slot}
							aria-busy={pending}
							aria-label={`hot cue ${entry.slot} deck ${deck.deck_id}`}
							data-testid={`hot-cue-${deck.deck_id}-${entry.slot}`}
							data-performance-control="hot-cue"
							data-proposal-kind={visible !== null ? visible.kind : undefined}
							title={entry.cue === null
								? deck.stable_id === null
									? NOT_LOADED_TIP
									: visible !== null
										? `${proposalTitle(visible.kind, visible.time_s)} - click to save the current position`
										: 'empty hot cue slot - click to save the current position'
								: hotCueTitle(entry.cue, deck.anlz?.beatgrid.beats ?? [], lyricsState.lyrics?.lines ?? [])}
							onclick={(e) => onSlotClick(entry, e.timeStamp)}
						>
							<span class="letter">{entry.slot}</span>
							{#if visible !== null}
								<HotCueProposalLabel kind={visible.kind} />
							{/if}
							{#if entry.cue !== null}
								<span class="cue-label">{entry.cue.comment ?? `CUE ${entry.slot}`}</span>
								<span
									class="edit"
									role="button"
									tabindex="0"
									aria-label={`rename hot cue ${entry.slot} deck ${deck.deck_id}`}
									data-testid={`edit-hot-cue-${deck.deck_id}-${entry.slot}`}
									title={`edit hot cue ${entry.slot} label`}
									onclick={(event) => void onEditClick(entry, event)}
									onkeydown={(event) => {
										if (event.key === 'Enter' || event.key === ' ') {
											event.preventDefault();
											void onEditClick(entry, event);
										}
									}}
								>
									&#9998;
								</span>
								<span
									class="clear"
									role="button"
									tabindex="0"
									aria-label={`clear hot cue ${entry.slot} deck ${deck.deck_id}`}
									data-testid={`clear-hot-cue-${deck.deck_id}-${entry.slot}`}
									title={`clear hot cue ${entry.slot}`}
									onclick={(event) => onClearClick(entry.slot, event)}
									onkeydown={(event) => {
										if (event.key === 'Enter' || event.key === ' ') {
											event.preventDefault();
											onClearClick(entry.slot, event as unknown as MouseEvent);
										}
									}}
								>
									&#215;
								</span>
							{/if}
						</button>
						{#if renameSlot === entry.slot}
							<!-- Rendered ABOVE the pad (position:absolute, bottom:100%) rather
							     than replacing it inline - the pin's "more space" ask - and
							     wide enough (width:max-content) that a long label is not
							     cramped into one grid track's width. -->
							<div class="cue-name-popover">
								<input
									class="cue-name"
									aria-label={`name hot cue ${entry.slot} deck ${deck.deck_id}`}
									data-testid={`hot-cue-name-${deck.deck_id}-${entry.slot}`}
									bind:this={renameInputEl}
									bind:value={renameDraft}
									onblur={(event) => {
						// Tabbing to the cancel button fires this blur BEFORE its click
						// (bot review P2, pin c20eeb07cae0): mousedown-preventDefault only
						// stops pointer activation, so a keyboard Tab+Enter/Space still
						// raced blur-save ahead of cancelRename. Skip the commit when
						// focus is headed straight to that button; its own click handles
						// cancellation instead.
						if (
							event.relatedTarget instanceof HTMLElement &&
							event.relatedTarget.classList.contains('cue-name-cancel')
						) {
							return;
						}
						void commitRename(entry);
					}}
									onkeydown={(event) => {
										if (event.key === 'Enter') {
											event.preventDefault();
											event.currentTarget.blur();
										} else if (event.key === 'Escape') {
											event.preventDefault();
											cancelRename(entry.slot);
										}
									}}
								/>
								<!-- svelte-ignore a11y_consider_explicit_label -->
								<button
									type="button"
									class="cue-name-cancel"
									title={`cancel - discard without saving`}
									aria-label={`cancel rename hot cue ${entry.slot} deck ${deck.deck_id}`}
									data-testid={`cancel-hot-cue-name-${deck.deck_id}-${entry.slot}`}
									onmousedown={(event) => event.preventDefault()}
									onclick={() => cancelRename(entry.slot)}
									onblur={(event) => {
										// Tab away from THIS button without activating it (the
										// input's own blur already deferred its commit to us,
										// above) must not leave an uncommitted draft popover
										// open forever - finish the deferred commit unless focus
										// is headed back into the name input itself.
										if (event.relatedTarget !== renameInputEl) void commitRename(entry);
									}}
								>
									&#215;
								</button>
							</div>
						{/if}
					</div>
				{/each}
			</div>
		{/each}
	</div>
	<button class="rb-lit-button rb-inert dropdown" disabled title={plannedTitle('hot-cue-menu')} aria-label={`hot cue menu deck ${deck.deck_id}`} data-testid={`hot-cue-menu-deck-${deck.deck_id}`}>
		HOT CUE <span class="caret">&#9662;</span>
	</button>
	{#if undo !== null}
		<!-- Undo is frequently the control a DJ clicks to LEAVE the open cue-name
		input, and that click's mousedown blurs the input, which starts the
		hot_cue_save that raises `pending` synchronously - before the browser
		delivers the click. Gating this button on `pending` would therefore eat
		the very click that started the write and make the operator press UNDO
		twice. Repeat presses are safe without it: undoLastMutation serializes
		on the same busy-slot tail and the second one finds its token spent. -->
		<button class="rb-lit-button undo" aria-busy={pending} aria-label={`undo hot cue deck ${deck.deck_id}`} title={`Undo last hot-cue change on slot ${undo.slot}`} data-testid={`undo-hot-cue-deck-${deck.deck_id}`} onclick={undoLastMutation}>
			UNDO {undo.slot}
		</button>
	{/if}
</div>

<style>
	.cue-area {
		display: flex;
		flex-direction: column;
		gap: 3px;
		min-height: 0;
		min-width: 0;
		flex: 1 1 auto;
		width: 100%;
		align-self: flex-start;
	}
	.bank {
		display: flex;
		gap: 3px;
		flex: 0 0 auto;
		min-height: 0;
	}
	/* DECKUX-03: greedy columns. flex-basis (not width) drives the 50/50 <->
	   80/20 animation so flex-grow/shrink staying 0 keeps the split exact. */
	.cue-col {
		display: flex;
		flex-direction: column;
		gap: 3px;
		min-width: 0;
		flex: 0 0 50%;
		transition: flex-basis 150ms ease;
	}
	.cue-col.grow { flex-basis: 80%; z-index: 2; background: var(--rb-panel); }
	.cue-col.shrink { flex-basis: 20%; overflow: hidden; }
	@media (prefers-reduced-motion: reduce) {
		.cue-col {
			transition: none;
		}
	}
	.slot-cell {
		position: relative;
		min-width: 0;
	}
	.slot {
		display: flex;
		align-items: center;
		gap: 6px;
		width: 100%; /* <button> shrinks to fit: long labels spilled, not ellipsized (#4082) */
		min-width: 0;
		height: 18px;
		box-sizing: border-box;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		font-weight: 600;
		text-align: left;
		padding: 1px 6px;
		opacity: 0.55;
		cursor: pointer;
	}
	.slot:disabled {
		cursor: default;
	}
	.slot .letter {
		flex: 0 0 auto;
	}
	.slot.filled {
		opacity: 1;
		color: var(--rb-text);
		border-left: 3px solid var(--rb-green);
	}
	.slot.filled .letter {
		color: var(--rb-green);
	}
	/* DECKUX-01/02: rendered above the pad it names (position:absolute,
	   bottom:100%) rather than replacing it inline, so a long label gets
	   more room than one grid track's width instead of being squeezed. */
	.cue-name-popover {
		position: absolute;
		left: 0;
		bottom: 100%;
		margin-bottom: 2px;
		z-index: 20;
		display: flex;
		align-items: stretch;
		gap: 2px;
		width: max-content;
		max-width: 220px;
	}
	.cue-name {
		flex: 1 1 auto;
		min-width: 120px;
		height: 18px;
		box-sizing: border-box;
		padding: 1px 6px;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		outline: 1px solid var(--rb-green);
		outline-offset: 0;
	}
	.cue-name-cancel {
		flex: 0 0 auto;
		width: 18px;
		height: 18px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		cursor: pointer;
	}
	.cue-name-cancel:hover,
	.cue-name-cancel:focus-visible {
		color: var(--rb-red);
		background: rgba(255, 255, 255, 0.08);
		outline: none;
	}
	.edit {
		flex: 0 0 auto;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 14px;
		height: 14px;
		border-radius: 2px;
		color: var(--rb-text-dim);
		cursor: pointer;
	}
	.edit:hover,
	.edit:focus-visible {
		color: var(--rb-accent);
		background: rgba(255, 255, 255, 0.08);
		outline: none;
	}
	.slot.filled.loop {
		border-left-color: var(--rb-orange);
	}
	.slot.filled.loop .letter {
		color: var(--rb-orange);
	}
	.slot.inert-mapping {
		cursor: default;
		opacity: 0.35;
	}
	.cue-label {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-weight: 400;
	}
	.clear {
		flex: 0 0 auto;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 14px;
		height: 14px;
		border-radius: 2px;
		color: var(--rb-text-dim);
		cursor: pointer;
	}
	.clear:hover,
	.clear:focus-visible {
		color: var(--rb-red);
		background: rgba(255, 255, 255, 0.08);
		outline: none;
	}
	.dropdown {
		align-self: flex-start;
		height: 18px;
		box-sizing: border-box;
	}
	.undo {
		align-self: flex-start;
		height: 18px;
		box-sizing: border-box;
	}
	.caret {
		color: var(--rb-text-dim);
	}
</style>
