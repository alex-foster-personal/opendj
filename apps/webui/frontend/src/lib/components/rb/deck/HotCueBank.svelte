<script lang="ts">
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
	import type { HotCueMutation } from '$lib/rb/api-rb';
	import type { DeckState } from '$lib/rb/deck-state-types';
	import type { HotCue, HotCueSlot } from '$lib/rb/hot-cue-types';

	const MAPPING_TIP = 'cues need a rekordbox mapping';

	let {
		deck,
		pending,
		onJump,
		onSave,
		onDelete,
		onRestore,
		inertTip
	}: {
		deck: DeckState;
		pending: boolean;
		onJump: (ms: number) => Promise<void>;
		onSave: (slot: HotCueSlot) => Promise<HotCueMutation>;
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
	let undo: { slot: HotCueSlot; revision: string; reversalId: string } | null = $state(null);

	function fmtMs(ms: number): string {
		const total = Math.floor(ms / 1000);
		const m = Math.floor(total / 60);
		const s = total % 60;
		return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
	}

	async function onSlotClick(entry: { slot: HotCueSlot; cue: HotCue | null }): Promise<void> {
		if (busySlot !== null) return;
		if (entry.cue === null && !deck.has_rb_mapping) return; // inert: see MAPPING_TIP
		busySlot = entry.slot;
		try {
			if (entry.cue !== null) {
				await onJump(entry.cue.in_ms);
			} else {
				setUndo(entry.slot, await onSave(entry.slot));
			}
		} finally {
			busySlot = null;
		}
	}

	async function onClearClick(slot: HotCueSlot, event: MouseEvent): Promise<void> {
		event.stopPropagation();
		if (busySlot !== null) return;
		busySlot = slot;
		try {
			setUndo(slot, await onDelete(slot));
		} finally {
			busySlot = null;
		}
	}

	function setUndo(slot: HotCueSlot, mutation: HotCueMutation): void {
		if (mutation.reversal === undefined) {
			throw new Error(`hot cue ${slot}: server omitted reversal token`);
		}
		undo = { slot, revision: mutation.revision, reversalId: mutation.reversal.reversal_id };
	}

	async function undoLastMutation(): Promise<void> {
		if (undo === null || busySlot !== null) return;
		const target = undo;
		busySlot = target.slot;
		try {
			await onRestore(target.slot, target.revision, target.reversalId);
			undo = null;
		} finally {
			busySlot = null;
		}
	}
</script>

<div class="cue-area">
	<div class="bank">
		{#each bank as entry (entry.slot)}
			<button
				class="slot"
				class:filled={entry.cue !== null}
				class:loop={entry.cue !== null && entry.cue.is_loop}
				class:inert-mapping={entry.cue === null && !deck.has_rb_mapping}
				disabled={busySlot !== null}
				aria-busy={pending}
				title={entry.cue === null
					? deck.has_rb_mapping
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
						aria-label={`clear hot cue ${entry.slot}`}
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
		{/each}
	</div>
	<button class="rb-lit-button rb-inert dropdown" disabled title={inertTip}>
		HOT CUE <span class="caret">&#9662;</span>
	</button>
	{#if undo !== null}
		<button class="rb-lit-button undo" disabled={pending || busySlot !== null} onclick={undoLastMutation}>
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
