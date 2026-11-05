<script lang="ts">
	// R-TAG-2, R-TAG-3, R-TT-3: suggests existing tags literally and by meaning.
	// A new name only ever becomes a tag through the one-click confirm, which
	// names the closest existing tags. Reads and writes the same routes an agent does.
	import { onMount, tick } from 'svelte';
	import { suggestTags, type TagSuggestion } from '$lib/rb/api-edit-suite';
	import { applyTagChange, reportTagFailure, tagState } from '$lib/rb/library-tags.svelte';
	import { normalizeTagName } from '$lib/rb/tag-display';
	import { clampToViewport } from '$lib/ui/clamp-to-viewport';

	const SUGGEST_DEBOUNCE_MS = 120;
	/** Query strings stay short: past this the server gets no exclusion list. */
	const MAX_EXCLUDED_TRACKS = 40;

	let {
		x,
		y,
		stableIds,
		typeId
	}: { x: number; y: number; stableIds: string[]; typeId: string | null } = $props();

	let input = $state<HTMLInputElement | null>(null);
	let query = $state('');
	let suggestions = $state<TagSuggestion[]>([]);
	let highlighted = $state(0);
	let creating = $state(false);
	let busy = $state(false);
	let timer: ReturnType<typeof setTimeout> | null = null;

	const typeRow = $derived(tagState.types.find((type) => type.id === typeId) ?? null);
	const noun = $derived(typeRow === null ? 'tag' : typeRow.singular.toLowerCase());
	const prefix = $derived(typeId === null ? '#' : '');
	const trimmed = $derived(query.trim());
	const exact = $derived(
		suggestions.some((s) => s.name_norm === normalizeTagName(trimmed)) ||
			tagState.catalog.some((tag) => tag.name_norm === normalizeTagName(trimmed))
	);
	const canCreate = $derived(trimmed !== '' && !exact);
	const position = $derived(
		clampToViewport(
			x,
			y,
			{ width: 240, height: 260 },
			{ width: window.innerWidth, height: window.innerHeight }
		)
	);

	function close(): void {
		tagState.editor = null;
	}

	async function refreshSuggestions(): Promise<void> {
		const excluded = stableIds.length <= MAX_EXCLUDED_TRACKS ? stableIds : [];
		try {
			suggestions = await suggestTags(query, typeId, excluded);
			highlighted = 0;
		} catch (exc) {
			reportTagFailure(`tag suggestions failed: ${String(exc)}`);
		}
	}

	function onInput(): void {
		creating = false;
		if (timer !== null) clearTimeout(timer);
		timer = setTimeout(() => void refreshSuggestions(), SUGGEST_DEBOUNCE_MS);
	}

	async function apply(name: string): Promise<void> {
		busy = true;
		try {
			await applyTagChange({ stable_ids: stableIds, add: [name], type: typeId });
			close();
		} catch (exc) {
			reportTagFailure(`tag not added: ${String(exc)}`);
		} finally {
			busy = false;
		}
	}

	function onKeydown(event: KeyboardEvent): void {
		event.stopPropagation();
		if (event.key === 'Escape') {
			if (creating) creating = false;
			else close();
		} else if (event.key === 'ArrowDown') {
			event.preventDefault();
			highlighted = Math.min(highlighted + 1, Math.max(suggestions.length - 1, 0));
		} else if (event.key === 'ArrowUp') {
			event.preventDefault();
			highlighted = Math.max(highlighted - 1, 0);
		} else if (event.key === 'Enter') {
			event.preventDefault();
			if (creating) void apply(trimmed);
			else if (suggestions[highlighted] !== undefined) void apply(suggestions[highlighted].name);
			else if (canCreate) creating = true;
		}
	}

	onMount(() => {
		void refreshSuggestions();
		void tick().then(() => input?.focus());
		return () => {
			if (timer !== null) clearTimeout(timer);
		};
	});
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="tag-scrim" onclick={close} oncontextmenu={close}></div>
<div
	class="tag-editor"
	role="dialog"
	aria-label={`Add ${noun}`}
	data-testid="tag-editor"
	style={`left:${position.x}px;top:${position.y}px`}
>
	<input
		bind:this={input}
		bind:value={query}
		type="text"
		placeholder={`Add ${noun}`}
		aria-label={`Add ${noun}`}
		oninput={onInput}
		onkeydown={onKeydown}
	/>
	{#if creating}
		<p class="ask" data-testid="tag-create-confirm">Create {prefix}{trimmed}?</p>
		{#if suggestions.length > 0}
			<p class="closest">
				Closest: {suggestions
					.slice(0, 3)
					.map((s) => `${prefix}${s.name}`)
					.join(' · ')}
			</p>
		{/if}
		<div class="actions">
			<button type="button" onclick={() => (creating = false)} disabled={busy}>Cancel</button>
			<button
				type="button"
				class="primary"
				data-testid="tag-create-yes"
				onclick={() => apply(trimmed)}
				disabled={busy}>Create</button
			>
		</div>
	{:else}
		<ul>
			{#each suggestions as suggestion, i (suggestion.name_norm)}
				<li>
					<button
						type="button"
						class:on={i === highlighted}
						onclick={() => apply(suggestion.name)}
						disabled={busy}
					>
						<span>{prefix}{suggestion.name}</span>
						{#if suggestion.similar}<em>similar</em>{/if}
						<small>{suggestion.track_count}</small>
					</button>
				</li>
			{/each}
			{#if canCreate}
				<li>
					<button
						type="button"
						data-testid="tag-create"
						onclick={() => (creating = true)}
						disabled={busy}>Create {prefix}{trimmed}</button
					>
				</li>
			{/if}
		</ul>
	{/if}
</div>

<style>
	.tag-scrim {
		position: fixed;
		inset: 0;
		z-index: 40;
	}
	.tag-editor {
		position: fixed;
		z-index: 41;
		width: 240px;
		padding: 6px;
		border: 1px solid var(--rb-border, #333);
		border-radius: 4px;
		background: var(--rb-panel-raised, #1c1f24);
		color: var(--rb-text, #ddd);
		font-size: 11px;
	}
	input {
		box-sizing: border-box;
		width: 100%;
		padding: 4px 6px;
		border: 1px solid var(--rb-border, #333);
		border-radius: 2px;
		background: var(--rb-bg, #14161a);
		color: inherit;
	}
	ul {
		max-height: 200px;
		margin: 4px 0 0;
		padding: 0;
		overflow-y: auto;
		list-style: none;
	}
	li button {
		display: flex;
		gap: 6px;
		align-items: baseline;
		width: 100%;
		padding: 3px 4px;
		border: 0;
		background: none;
		color: inherit;
		font: inherit;
		text-align: left;
		cursor: pointer;
	}
	li button:hover,
	li button.on {
		background: var(--rb-select, #2a3a55);
	}
	li span {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-style: italic;
	}
	em,
	small,
	.closest {
		color: var(--rb-text-dim);
	}
	.ask,
	.closest {
		margin: 6px 2px 0;
	}
	.actions {
		display: flex;
		justify-content: flex-end;
		gap: 6px;
		margin-top: 6px;
	}
</style>
