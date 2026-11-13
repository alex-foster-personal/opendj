<script lang="ts">
	// HOT CUE pad-mode dropdown (CHROME-11, issue #3886). Deck.svelte mounts
	// it `stacked` in the left grid column under grid adjust/shift, where the
	// label wraps to two lines, HOT over CUE (LYR-14). Lists the modes a hardware controller's performance-pad
	// selector normally exposes. Hot Cue, Beat Jump, Pad FX 1 and Pad FX 2 are built;
	// picking one dispatches the typed pad_mode command (performance-ipc).
	// Every other entry is labeled not-built-yet, and any entry that cannot be
	// selected (unbuilt, or Pad FX with no beat FX unit) renders inert with its
	// reason as the tooltip, per the house rule for controls with no real backing.
	//
	// The deck and its main row clip overflow (Deck.svelte), so an absolute
	// menu below the bank was cut off. The menu is position: fixed and
	// placed from the trigger's rect by triggerFloatingAction (placeFloating
	// under the hood): below when it fits, flipped above otherwise, clamped
	// to the viewport.
	//
	// STRUCT-9657: a Segments entry under the modes opens the section list
	// (SegmentsList.svelte): a row click dispatches the agent `seek` verb, and
	// "+ hot cue" the agent `hot_cue_save` verb, both via the same command path.
	import type { DeckId } from '$lib/rb/deck-id';
	import type { HotCueSlot } from '$lib/rb/hot-cue-types';
	import { sectionsApiFor } from '$lib/components/rb/wave/sections-lazy.svelte';
	import SegmentsList from './SegmentsList.svelte';
	import { PAD_MODE_CATALOG, padModeMenuLabel } from '$lib/rb/pad-mode-catalog';
	import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';
	import { getDeckState, runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';
	import { padModeFor, padModeSelectable } from '$lib/rb/pad-mode-state.svelte';
	import { selectorNode } from '$lib/keyboard/selector-register';
	import { padModeMenuSelId } from './deck-pad-selector-ids';
	import type { HotCueMutation } from '$lib/rb/api-rb';
	import { pushToast, pushToastForFailure } from '$lib/stores.svelte';
	import {
		cueListHighlights,
		cueListInertReason,
		cueListRows,
		cueRowCommand,
		deckCueListSources,
		lastPadMenuTab,
		rememberPadMenuTab,
		type CueListRow,
		type PadMenuTab
	} from '$lib/rb/cue-list';

	// DECKUX-9981: the Cues tab lists the loaded track's memory cues, hot cues
	// and loops (lib/rb/cue-list.ts), read from the same live deck state the
	// Segments entry uses; hot-cue x uses the bank's clear path and offers UNDO.
	let {
		deckId,
		stacked = false,
		onDeleteHotCue,
		onRestoreHotCue
	}: {
		deckId: DeckId;
		stacked?: boolean;
		onDeleteHotCue?: (slot: HotCueSlot) => Promise<HotCueMutation>;
		onRestoreHotCue?: (slot: HotCueSlot, revision: string, reversalId: string) => Promise<void>;
	} = $props();

	const deck = $derived(getDeckState(deckId));
	let open = $state(false);
	let tab: PadMenuTab = $state('modes');
	const cueInertReason = $derived(cueListInertReason(deck.stable_id ?? null));
	const cueRows = $derived(
		cueInertReason !== null
			? []
			: cueListRows(deckCueListSources(deck.anlz?.cues ?? [], deck.hot_cues), deck.anlz?.beatgrid.beats ?? [])
	);
	const highlights = $derived(cueListHighlights(cueRows, deck.position_ms, deck.loop));

	function pickTab(next: PadMenuTab): void {
		tab = next;
		rememberPadMenuTab(deckId, next);
	}

	async function deleteHotCueRow(row: CueListRow): Promise<void> {
		const slot = row.slot;
		if (slot === null || onDeleteHotCue === undefined) return;
		try {
			const mutation = await onDeleteHotCue(slot);
			const reversal = mutation.reversal;
			if (reversal === undefined || onRestoreHotCue === undefined) return;
			pushToast(`Hot cue ${slot} cleared`, 'info', undefined, undefined, {}, undefined, {
				label: 'Undo',
				handler: () => void onRestoreHotCue(slot, mutation.revision, reversal.reversal_id).catch(pushToastForFailure)
			});
		} catch (exc) {
			pushToastForFailure(exc, `Hot cue ${slot}: clear failed`);
		}
	}
	const mode = $derived(padModeFor(deckId));
	const triggerLabel = $derived((PAD_MODE_CATALOG.find((entry) => entry.id === mode)?.label ?? 'Hot Cue').toUpperCase());
	let segmentsOpen = $state(false);
	const sectionsApi = $derived(sectionsApiFor(open && segmentsOpen));
	const spans = $derived(sectionsApi !== null && deck.anlz !== null ? sectionsApi.sectionSpans(deck.anlz.phrases) : []);
	let wrapEl: HTMLSpanElement | undefined = $state();

	function saveSegmentHotCue(slot: HotCueSlot, positionMs: number): void {
		const revision = deck.hot_cue_revisions[slot];
		void runPerformanceCommandFromUi({ type: 'hot_cue_save', deck: deckId, slot, in_ms: positionMs, revision });
	}
	let triggerEl: HTMLButtonElement | undefined = $state();

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
</script>

<svelte:window onpointerdown={onWindowPointerDown} onkeydown={onWindowKeyDown} />

<span class="pad-menu-wrap" bind:this={wrapEl}>
	<button
		bind:this={triggerEl}
		class="rb-lit-button dropdown"
		class:stacked
		aria-label={`hot cue menu deck ${deckId}`}
		title="Pad mode menu - lists the pad modes a controller's performance-pad selector offers; modes marked not-built-yet do nothing yet"
		aria-expanded={open}
		data-testid={`hot-cue-menu-deck-${deckId}`}
		use:selectorNode={{ id: padModeMenuSelId(deckId), kind: 'leaf', enabled: true }}
		onclick={() => {
			// DECKUX-9981: reopen on the tab last picked this session.
			if (!open) tab = lastPadMenuTab(deckId);
			open = !open;
		}}
	>
		{triggerLabel}
	</button>
	{#if open}
		<div
			class="pad-menu"
			role="menu"
			use:triggerFloatingAction={{ getTrigger: () => triggerEl ?? null, preferred: 'below', gap: 4 }}
		>
			{#if deck !== null}
				<div class="pad-menu-tabs" role="tablist">
					<button type="button" role="tab" class="pad-menu-tab" aria-selected={tab === 'modes'} onclick={() => pickTab('modes')}>Modes</button>
					<button
						type="button"
						role="tab"
						class="pad-menu-tab"
						class:rb-inert={cueInertReason !== null}
						aria-selected={tab === 'cues'}
						title={cueInertReason ?? 'Memory cues, hot cues and loops of the loaded track'}
						data-testid={`cues-tab-deck-${deckId}`}
						onclick={() => pickTab('cues')}>Cues</button
					>
				</div>
			{/if}
			{#if tab === 'cues'}
				{#if cueInertReason !== null}
					<div class="cue-list-empty rb-inert" title={cueInertReason}>{cueInertReason}</div>
				{:else if cueRows.length === 0}
					<div class="cue-list-empty">No stored cues on this track</div>
				{:else}
					{#each cueRows as row, index (row.key)}
						<div
							class="cue-row"
							class:engaged={index === highlights.engagedIndex}
							class:nearest={index === highlights.nearestIndex}
							data-cue-kind={row.kind}
						>
							<button
								type="button"
								class="pad-menu-item cue-row-main"
								role="menuitem"
								onclick={() => void runPerformanceCommandFromUi(cueRowCommand(deckId, row))}
							>
								<span class="cue-kind">{row.kind === 'hot_cue' ? row.slot : row.kind === 'loop' ? 'LOOP' : 'MEM'}</span>
								<span class="cue-time">{row.time}</span>
								{#if row.isLoop}<span class="cue-beats">{row.loopBeats === null ? '-' : `${row.loopBeats} beats`}</span>{/if}
								{#if row.comment}<span class="cue-comment">{row.comment}</span>{/if}
							</button>
							{#if row.kind === 'hot_cue'}
								<button type="button" class="cue-row-x" aria-label={`clear hot cue ${row.slot}`} title={`Clear hot cue ${row.slot}`} onclick={() => void deleteHotCueRow(row)}>x</button>
							{:else}
								<button type="button" class="cue-row-x rb-inert" disabled title="Memory cue delete needs the delete route (issue #9762), not built yet">x</button>
							{/if}
						</div>
					{/each}
				{/if}
			{:else}
			{#each PAD_MODE_CATALOG as entry (entry.id)}
				{@const sel = padModeSelectable(deckId, entry)}
				<button
					type="button"
					class="pad-menu-item"
					class:rb-inert={!sel.enabled}
					role="menuitem"
					data-pad-mode={entry.id}
					data-built={entry.built}
					disabled={!sel.enabled}
					title={sel.enabled ? undefined : sel.reason}
					onclick={() => {
						open = false;
						void runPerformanceCommandFromUi({ type: 'pad_mode', deck: deckId, mode: entry.id });
					}}
				>
					{padModeMenuLabel(entry)}
				</button>
			{/each}
			<button
				type="button"
				class="pad-menu-item segments-entry"
				role="menuitemcheckbox"
				aria-checked={segmentsOpen}
				data-testid={`segments-entry-deck-${deckId}`}
				onclick={() => (segmentsOpen = !segmentsOpen)}
			>
				Segments
			</button>
			{#if segmentsOpen}
				<SegmentsList
					{spans}
					beats={deck.anlz?.beatgrid.beats ?? null}
					positionMs={deck.position_ms}
					hotCues={deck.hot_cues}
					onSeek={(positionMs) => void runPerformanceCommandFromUi({ type: 'seek', deck: deckId, position_ms: positionMs })}
					onSaveHotCue={saveSegmentHotCue}
				/>
			{/if}
			{/if}
		</div>
	{/if}
</span>

<style>
	.dropdown {
		align-self: flex-start;
		height: 18px;
		box-sizing: border-box;
	}
	/* CHROME-01: the caret is a CSS border triangle, never a text glyph. */
	.dropdown::after {
		content: '';
		display: inline-block;
		width: 0;
		height: 0;
		/* the gap the space before a glyph used to leave */
		margin-left: 5px;
		vertical-align: middle;
		border-left: 3px solid transparent;
		border-right: 3px solid transparent;
		border-top: 4px solid var(--rb-text-dim);
	}
	.pad-menu-wrap {
		position: relative;
		align-self: flex-start;
	}
	.pad-menu {
		/* fixed escapes the deck's overflow: hidden; left/top are set by
		 * triggerFloatingAction from the trigger rect */
		position: fixed;
		z-index: 1000;
		min-width: 180px;
		background: var(--rb-panel);
		border: 1px solid var(--rb-border);
		border-radius: 4px;
		padding: 4px 0;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.45);
	}
	.pad-menu-item {
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
	.pad-menu-item:hover:not(.rb-inert) {
		background: rgba(255, 255, 255, 0.06);
	}
	.segments-entry {
		border-top: 1px solid var(--rb-border);
	}
	.pad-menu-item.rb-inert {
		opacity: 0.55;
		cursor: default;
	}
	.pad-menu-tabs {
		display: flex;
		gap: 2px;
		padding: 0 4px 4px;
		border-bottom: 1px solid var(--rb-border);
		margin-bottom: 4px;
	}
	.pad-menu-tab {
		flex: 1;
		background: transparent;
		border: none;
		color: var(--rb-text-dim);
		font-size: 10px;
		padding: 3px 6px;
		cursor: pointer;
	}
	.pad-menu-tab[aria-selected='true'] {
		color: var(--rb-text);
		border-bottom: 1px solid var(--rb-text);
	}
	.cue-row {
		display: flex;
		align-items: center;
	}
	.cue-row.nearest {
		background: rgba(255, 255, 255, 0.04);
	}
	.cue-row.engaged {
		background: rgba(255, 170, 0, 0.18);
	}
	.cue-row-main {
		display: flex;
		gap: 6px;
		flex: 1;
	}
	.cue-kind {
		min-width: 30px;
		color: var(--rb-text-dim);
	}
	.cue-comment {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.cue-row-x {
		background: transparent;
		border: none;
		color: var(--rb-text-dim);
		font-size: 11px;
		padding: 0 8px;
		cursor: pointer;
	}
	.cue-row-x.rb-inert {
		opacity: 0.4;
		cursor: default;
	}
	.cue-list-empty {
		font-size: 11px;
		color: var(--rb-text-dim);
		padding: 5px 10px;
	}
	/* LYR-14: two lines in the narrow grid column. width: min-content wraps
	   the label at its space, so the text stays "HOT CUE" for readers and
	   tests while it draws as HOT over CUE, caret under it. */
	.dropdown.stacked {
		height: auto;
		width: min-content;
		padding: 2px 3px 3px;
		line-height: 1.05;
		font-size: 8.5px;
		letter-spacing: 0.04em;
		text-align: center;
	}
	.dropdown.stacked::after {
		display: block;
		margin: 2px auto 0;
	}
</style>
