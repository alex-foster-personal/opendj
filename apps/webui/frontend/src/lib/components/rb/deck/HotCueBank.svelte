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
	// A locally imported track has no djmdContent row for djmdCue to key
	// off, so SAVE has nowhere to write (PARITY-TODO v1 blocker, issue
	// #736): every slot on such a deck (deck.has_rb_mapping false) always
	// reads empty from the server and goes inert-with-tooltip rather than
	// firing a write that would 404.
	//
	// An empty deck (deck.stable_id null - nothing loaded, or momentarily
	// mid-reload) has no track to save onto either, and its has_rb_mapping
	// defaults true (deck-state-types.ts), so that flag alone cannot gate
	// this case. Same inert-with-tooltip treatment, gated on stable_id
	// instead (issue #804).
	import type { HotCueMutation } from '$lib/rb/api-rb';
	import type { DeckState } from '$lib/rb/deck-state-types';
	import type { HotCue, HotCueSlot } from '$lib/rb/hot-cue-types';

	const MAPPING_TIP = 'cues need a rekordbox mapping';
	const NOT_LOADED_TIP = 'no track loaded - nothing to save';

	let {
		deck,
		pending,
		onJump,
		onSave,
		onRename,
		onDelete,
		onRestore,
		inertTip
	}: {
		deck: DeckState;
		pending: boolean;
		/** #884: slot-addressed, not a raw ms - lets the dispatcher honour
		 * BeatSyncMax (arm for the deck's own next downbeat) instead of a plain
		 * unconditional seek. */
		onJump: (slot: HotCueSlot) => Promise<void>;
		onSave: (slot: HotCueSlot, comment?: string, fixedPositionMs?: number, quantizeFixedPosition?: boolean, expectedStableId?: string) => Promise<HotCueMutation>;
		onRename: (slot: HotCueSlot, inMs: number, comment: string) => Promise<HotCueMutation>;
		onDelete: (slot: HotCueSlot) => Promise<HotCueMutation>;
		onRestore: (slot: HotCueSlot, revision: string, reversalId: string) => Promise<void>;
		inertTip: string;
	} = $props();

	const SLOTS: HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

	const bank: { slot: HotCueSlot; cue: HotCue | null }[] = $derived(
		SLOTS.map((slot) => ({
			slot,
			cue: deck.hot_cues.find((c) => c.slot === slot) ?? null
		}))
	);

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

	function fmtMs(ms: number): string {
		const total = Math.floor(ms / 1000);
		const m = Math.floor(total / 60);
		const s = total % 60;
		return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
	}

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

	async function onSlotClick(entry: { slot: HotCueSlot; cue: HotCue | null }): Promise<void> {
		if (entry.cue !== null) {
			await onJump(entry.slot);
			return;
		}
		// Empty deck rows are inert (WaveRow.svelte precedent): has_rb_mapping
		// defaults true on an empty deck (deck-state-types.ts), so it alone
		// cannot gate the save - stable_id is the real "is there a deck to
		// save onto" signal (#804).
		if (deck.stable_id === null || !deck.has_rb_mapping) return;
		await beginRename(entry.slot, deck.position_ms, deck.stable_id);
	}

	async function beginRename(slot: HotCueSlot, newCueAtMs: number | null, stableId: string): Promise<void> {
		renameSlot = slot;
		renameNewCueAtMs = newCueAtMs;
		renameStableId = stableId;
		renameDraft = '';
		await tick();
		if (renameInputEl === null) throw new Error(`hot cue ${slot}: name input did not mount`);
		renameInputEl.focus();
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
		{#each bank as entry (entry.slot)}
			{#if renameSlot === entry.slot}
				<input
					class="slot cue-name"
					class:filled={entry.cue !== null}
					class:loop={entry.cue !== null && entry.cue.is_loop}
					aria-label={`name hot cue ${entry.slot} deck ${deck.deck_id}`}
					data-testid={`hot-cue-name-${deck.deck_id}-${entry.slot}`}
					bind:this={renameInputEl}
					bind:value={renameDraft}
					onblur={() => void commitRename(entry)}
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
			{:else}
			<button
				class="slot"
				class:filled={entry.cue !== null}
				class:loop={entry.cue !== null && entry.cue.is_loop}
				class:inert-mapping={entry.cue === null &&
					(deck.stable_id === null || !deck.has_rb_mapping)}
				disabled={busySlot === entry.slot}
				aria-busy={pending}
				aria-label={`hot cue ${entry.slot} deck ${deck.deck_id}`}
				data-testid={`hot-cue-${deck.deck_id}-${entry.slot}`}
				data-performance-control="hot-cue"
				title={entry.cue === null
					? deck.stable_id === null
						? NOT_LOADED_TIP
						: deck.has_rb_mapping
							? 'empty hot cue slot - click to save the current position'
							: MAPPING_TIP
					: (entry.cue.comment ?? `hot cue ${entry.slot}`)}
				onclick={() => onSlotClick(entry)}
			>
				<span class="letter">{entry.slot}</span>
				{#if entry.cue !== null}
					<span class="cue-label">{entry.cue.comment ?? `CUE ${entry.slot}`}</span>
					<span class="cue-time">{fmtMs(entry.cue.in_ms)}</span>
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
			{/if}
		{/each}
	</div>
	<button class="rb-lit-button rb-inert dropdown" disabled title={inertTip} aria-label={`hot cue menu deck ${deck.deck_id}`} data-testid={`hot-cue-menu-deck-${deck.deck_id}`}>
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
		<button class="rb-lit-button undo" aria-busy={pending} aria-label={`undo hot cue deck ${deck.deck_id}`} data-testid={`undo-hot-cue-deck-${deck.deck_id}`} onclick={undoLastMutation}>
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
		display: grid;
		grid-template-rows: repeat(4, 18px);
		grid-template-columns: repeat(2, minmax(0, 1fr));
		grid-auto-flow: column;
		gap: 3px;
		flex: 0 0 auto;
		min-height: 0;
	}
	.slot {
		display: flex;
		align-items: center;
		gap: 6px;
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
	.cue-name {
		outline: 1px solid var(--rb-green);
		outline-offset: 0;
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
	.cue-time {
		flex: 0 0 auto;
		color: var(--rb-text-dim);
		font-weight: 400;
		font-variant-numeric: tabular-nums;
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
