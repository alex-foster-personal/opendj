<script lang="ts">
	/**
	 * Edit sample modal (spec 7.4, mock `openSlotPop`): the name and description behind
	 * pencils (Enter or blur saves, Escape cancels), the source line always shown, the
	 * CUE-only deck, the captured mix chips (or one neutral line), the play info line, the
	 * settings rows (SampleSettingsRows) and one explainer row for the hovered or focused
	 * control. 600 px wide, beside the tab, never over it. Edits go through the store's `updateSlot`, so the tab renames at once.
	 * A pointer press anywhere else or Escape (outside an input) closes it.
	 */
	import { HEAD_OPEN_W } from '$lib/player/sampler/edge-wave';
	import {
		DESCRIPTION_MAX,
		NAME_MAX,
		cueInfo,
		editKeyAction,
		editPatch,
		playInfo,
		sourceLine,
		vectorChips,
		type EditField
	} from '$lib/player/sampler/sample-edit';
	import { keyPlanFor, stemsPending } from '$lib/player/sampler/sampler-store-logic';
	import {
		sampler,
		slotPhase,
		slotPlayhead,
		updateSlot
	} from '$lib/player/sampler/sampler-store.svelte';
	import { displayName, type Edge, type SamplerSlot } from '$lib/player/sampler/sampler-types';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { PENCIL_PATH } from '$lib/ui/icon-glyphs';
	import CueOnlyDeck from './CueOnlyDeck.svelte';
	import SampleSettingsRows from './SampleSettingsRows.svelte';

	let {
		edge,
		slot,
		anchorY,
		areaHeight,
		onclose
	}: {
		edge: Edge;
		slot: SamplerSlot;
		/** the edited tab's centre, px from the edge column's top */
		anchorY: number;
		areaHeight: number;
		onclose: () => void;
	} = $props();

	const IDLE = 'Hover a setting to see what it does.';
	/** vectorChips' stem chips start with a part label. */
	const STEM_CHIP = /^(Vocals|Drums|Bass|Inst) /;
	const side = `${HEAD_OPEN_W + 10}px`;

	let popEl: HTMLDivElement | undefined = $state();
	let deck: CueOnlyDeck | undefined = $state();
	let popH = $state(560);
	let explain = $state(IDLE);
	let editing = $state<EditField | null>(null);
	let draft = $state('');

	const plan = $derived(keyPlanFor(slot, sampler.master, uiPrefs.compatible_filter));
	const masterBpm = $derived(sampler.master?.bpm ?? null);
	const src = $derived(sourceLine(slot));
	const top = $derived(Math.max(8, Math.min(areaHeight - 8 - popH, anchorY - 220)));

	$effect(() => {
		if (!popEl) return;
		const ro = new ResizeObserver(() => popEl && (popH = popEl.offsetHeight));
		ro.observe(popEl);
		return () => ro.disconnect();
	});

	/** Move the CUE deck's playhead; true while this slot previews (the overlay's frame loop calls it). */
	export function paint(): boolean {
		const f = slotPhase(slot.slot_id) === 'previewing' ? slotPlayhead(slot.slot_id) : null;
		if (deck === undefined || f === null) return false;
		deck.paint(f);
		return true;
	}

	function startEdit(field: EditField): void {
		editing = field;
		draft = field === 'name' ? displayName(slot) : slot.description;
	}
	function saveEdit(): void {
		const field = editing;
		if (field === null) return;
		editing = null;
		const patch = editPatch(slot, field, draft);
		// updateSlot toasts and rolls back on failure; the catch stops a second, unhandled report.
		if (patch !== null) void updateSlot(slot.slot_id, patch).catch(() => undefined);
	}
	function onEditKey(ev: KeyboardEvent, field: EditField): void {
		const action = editKeyAction(field, ev.key);
		if (action === 'save') {
			ev.preventDefault();
			saveEdit();
		} else if (action === 'cancel') {
			ev.stopPropagation();
			editing = null;
		}
	}
	function focusSelect(node: HTMLInputElement | HTMLTextAreaElement): void {
		node.focus();
		node.select();
	}
	function showTip(ev: Event): void {
		const t = (ev.target as HTMLElement).closest<HTMLElement>('[data-tip]');
		if (t && popEl?.contains(t)) explain = t.dataset.tip ?? IDLE;
	}
	function hideTip(ev: PointerEvent): void {
		const next = ev.relatedTarget as HTMLElement | null;
		if (!next || !popEl?.contains(next) || !next.closest('[data-tip]')) explain = IDLE;
	}
</script>

{#snippet pencil(field: EditField, label: string, tip: string)}
	<button
		type="button"
		class="epen"
		aria-label={label}
		data-tip={tip}
		onclick={() => startEdit(field)}
		><svg viewBox="0 0 24 24" width="12" height="12" aria-hidden="true"
			><path d={PENCIL_PATH} fill="none" stroke="currentColor" stroke-width="2" /></svg
		></button
	>
{/snippet}

<svelte:window onpointerdown={onclose} />
<div
	bind:this={popEl}
	class="pop edit"
	role="dialog"
	aria-label="Edit sample"
	tabindex="-1"
	data-testid="edit-sample-modal"
	style:top="{top}px"
	style:right={edge === 'R' ? side : undefined}
	style:left={edge === 'L' ? side : undefined}
	onpointerdown={(ev) => ev.stopPropagation()}
	onpointerover={showTip}
	onpointerout={hideTip}
	onfocusin={showTip}
	onkeydown={(ev) => {
		if (ev.key === 'Escape' && editing === null) onclose();
	}}
>
	<h3>Edit sample</h3>
	<div class="erow">
		{#if editing === 'name'}
			<input
				type="text"
				maxlength={NAME_MAX}
				aria-label="Sample name"
				placeholder="Sample name"
				bind:value={draft}
				use:focusSelect
				onblur={saveEdit}
				onkeydown={(ev) => onEditKey(ev, 'name')}
			/>
		{:else}
			<span class="val name">{displayName(slot)}</span>
			{@render pencil(
				'name',
				'Edit sample name',
				'Rename the sample. The source track stays shown below.'
			)}
		{/if}
	</div>
	<div class="erow">
		{#if editing === 'description'}
			<textarea
				maxlength={DESCRIPTION_MAX}
				aria-label="Description"
				placeholder="Add a description"
				bind:value={draft}
				use:focusSelect
				onblur={saveEdit}
				onkeydown={(ev) => onEditKey(ev, 'description')}></textarea>
		{:else}
			<span class="val desc" class:empty={!slot.description}
				>{slot.description || 'Add a description'}</span
			>
			{@render pencil(
				'description',
				'Edit description',
				'A short note for this sample, shown on hover.'
			)}
		{/if}
	</div>
	<div
		class="src"
		data-tip="The track and cue this sample was taken from. Always shown, even after you rename the sample."
	>
		Source: <b>{src.title}</b>{src.rest}
	</div>
	<CueOnlyDeck bind:this={deck} {slot} info={cueInfo(masterBpm, plan)} />
	{#if slot.vector !== null}
		<div
			class="vec"
			data-tip="The mixer settings this loop had when it was dragged in. The sample always plays with them."
		>
			{#each vectorChips(slot.vector) as chip (chip)}
				{#if stemsPending(slot) && STEM_CHIP.test(chip)}
					<span class="pending" title="Stems pending: plays once this track's stem-set exists"
						>{chip} (pending)</span
					>
				{:else}
					<span>{chip}</span>
				{/if}
			{/each}
		</div>
	{:else}
		<div class="sum">
			Neutral mix (saved loop hot cue). Drag a loop from a deck to keep its EQ, filter and stem
			settings.
		</div>
	{/if}
	<div class="sum">{playInfo(slot, masterBpm, plan)}</div>
	<SampleSettingsRows {slot} />
	<div class="explain" class:idle={explain === IDLE}>{explain}</div>
</div>

<style>
	.pop {
		position: absolute;
		z-index: 9;
		pointer-events: auto;
		box-sizing: border-box;
		width: 600px;
		display: grid;
		gap: 10px;
		padding: 12px 14px;
		background: var(--es-panel);
		border: 1px solid var(--es-border-strong);
		border-radius: 7px;
		box-shadow: 0 14px 44px var(--es-shadow-pop);
		font: 13px var(--es-font-body);
		color: var(--es-fg);
	}
	h3 {
		margin: 0;
		font: 18px var(--es-font-display);
		letter-spacing: 0.5px;
		text-transform: uppercase;
	}
	.erow {
		display: flex;
		align-items: baseline;
		gap: 8px;
		min-width: 0;
	}
	.val {
		min-width: 0;
		flex: 1;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.val.name {
		font: 22px var(--es-font-display);
		letter-spacing: 0.4px;
		white-space: nowrap;
	}
	.val.desc {
		font-size: 13px;
		color: var(--es-fg);
	}
	.val.empty {
		color: var(--es-dim);
		font-style: italic;
	}
	.epen {
		background: transparent;
		border: 1px solid transparent;
		color: var(--es-muted);
		border-radius: 4px;
		width: 26px;
		height: 24px;
		cursor: pointer;
		padding: 0;
		flex: none;
	}
	.epen:hover {
		color: var(--es-fg);
		border-color: var(--es-border-strong);
	}
	input,
	textarea {
		flex: 1;
		min-width: 0;
		background: var(--es-input-bg);
		color: var(--es-fg);
		border: 1px solid var(--es-line);
		border-radius: 4px;
		padding: 4px 7px;
		font: 13px var(--es-font-body);
	}
	textarea {
		min-height: 64px;
		resize: vertical;
	}
	.src {
		font: 12px var(--es-font-mono);
		color: var(--es-muted);
		background: var(--es-source-bg);
		border: 1px solid var(--es-line);
		border-radius: 4px;
		padding: 6px 8px;
	}
	.src b {
		color: var(--es-fg);
		font-weight: 500;
	}
	.vec {
		display: flex;
		flex-wrap: wrap;
		gap: 5px;
	}
	.vec span {
		font: 11.5px var(--es-font-mono);
		color: var(--es-deck-a);
		border: 1px solid var(--es-vec-border);
		border-radius: 3px;
		padding: 1px 6px;
	}
	.vec span.pending {
		opacity: 0.55;
		border-style: dashed;
	}
	.sum {
		font: 12px var(--es-font-mono);
		color: var(--es-muted);
	}
	.explain {
		border-top: 1px solid var(--es-line);
		padding-top: 8px;
		font-size: 12.5px;
		line-height: 1.35;
		color: var(--es-fg);
		min-height: 36px;
	}
	.explain.idle {
		color: var(--es-dim);
	}
</style>
