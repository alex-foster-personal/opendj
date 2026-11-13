<script lang="ts">
	/**
	 * Name and describe a set (spec 7.4, mock `openSetModal`, EDGESAMP-11872l), opened by a
	 * right-click on the top tab and placed beside it (476 + 10 px from the edge, 8 px down).
	 * Save goes through the store's `nameSet` (PUT /api/v1/sampler/sets/{id}), which toasts
	 * "Set named X"; an empty name or a refused save keeps the dialog open with the reason.
	 * A pointer press anywhere else, Cancel or Esc closes it.
	 */
	import { onMount, untrack } from 'svelte';
	import { HEAD_OPEN_W } from '$lib/player/sampler/edge-wave';
	import { nameSet } from '$lib/player/sampler/sampler-store.svelte';
	import type { SampleSet } from '$lib/player/sampler/sampler-sets';
	import type { Edge, SamplerSlot } from '$lib/player/sampler/sampler-types';

	let { edge, set, onclose }: { edge: Edge; set: SampleSet<SamplerSlot>; onclose: () => void } =
		$props();

	// The dialog edits a draft of the set as it was when opened.
	let name = $state(untrack(() => set.name));
	let description = $state(untrack(() => set.description));
	let status = $state('');
	let saving = $state(false);
	let nameEl: HTMLInputElement | undefined = $state();
	const side = `${HEAD_OPEN_W + 10}px`;

	onMount(() => {
		nameEl?.focus();
		nameEl?.select();
	});

	async function save(): Promise<void> {
		if (name.trim() === '') {
			status = 'A set needs a name.';
			return;
		}
		saving = true;
		try {
			await nameSet(set.id, name, description);
			onclose();
		} catch (err) {
			status = `Not saved: ${err instanceof Error ? err.message : String(err)}`;
		} finally {
			saving = false;
		}
	}
</script>

<svelte:window onpointerdown={onclose} />
<div
	class="pop"
	role="dialog"
	tabindex="-1"
	aria-label="Name this set"
	style:top="8px"
	style:right={edge === 'R' ? side : undefined}
	style:left={edge === 'L' ? side : undefined}
	onpointerdown={(ev) => ev.stopPropagation()}
	oncontextmenu={(ev) => ev.stopPropagation()}
	onkeydown={(ev) => {
		if (ev.key === 'Escape') onclose();
	}}
>
	<h3>Name this set</h3>
	<label class="lbl" for="sampler-set-name-{edge}">Name</label>
	<input
		id="sampler-set-name-{edge}"
		type="text"
		maxlength="40"
		bind:this={nameEl}
		bind:value={name}
		onkeydown={(ev) => ev.key === 'Enter' && void save()}
	/>
	<label class="lbl" for="sampler-set-desc-{edge}">Description</label>
	<textarea id="sampler-set-desc-{edge}" maxlength="200" bind:value={description}></textarea>
	<div class="acts">
		<button type="button" class="cancel" onclick={onclose}>Cancel</button>
		<button type="button" class="go" disabled={saving} onclick={() => void save()}>Save</button>
	</div>
	<div class="sum" role="status">{status}</div>
</div>

<style>
	.pop {
		position: absolute;
		z-index: 9;
		pointer-events: auto;
		box-sizing: border-box;
		width: 330px;
		display: grid;
		gap: 9px;
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
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.lbl {
		font: 13px var(--es-font-display);
		letter-spacing: 0.6px;
		text-transform: uppercase;
		color: var(--es-muted);
	}
	input,
	textarea {
		box-sizing: border-box;
		width: 100%;
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
	.acts {
		display: flex;
		gap: 6px;
		justify-content: flex-end;
	}
	.acts button {
		font: 15px var(--es-font-display);
		letter-spacing: 0.5px;
		text-transform: uppercase;
		border-radius: 4px;
		padding: 3px 12px;
		cursor: pointer;
		border: 1px solid var(--es-line);
		background: var(--es-raised);
		color: var(--es-fg);
	}
	.acts button.go {
		background: var(--es-accent);
		color: var(--es-accent-ink);
		border-color: var(--es-accent);
	}
	.sum {
		font: 12px var(--es-font-mono);
		color: var(--es-muted);
	}
	.sum:empty {
		display: none;
	}
</style>
