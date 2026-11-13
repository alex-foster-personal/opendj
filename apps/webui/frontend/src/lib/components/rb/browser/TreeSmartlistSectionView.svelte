<script lang="ts">
	// TreeSmartlistSection's rows as props only: the container owns the
	// smartlist rows, the inline rename and the tree context menu; the rename
	// input element is bound back to it so a begun rename can focus it.
	import type { SmartlistSummary } from '$lib/rb/api-smartlists';
	import { SMARTLIST_DROP_REFUSAL } from '$lib/rb/track-drag.svelte';

	let {
		open,
		rows,
		error,
		selectedId,
		selectable,
		editingId,
		editDraft,
		renameInputEl = $bindable(null),
		refusedId = null,
		ontoggle,
		onclickrow,
		ondblclickrow = () => {},
		onmenukey,
		oncontextmenurow,
		oneditdraft,
		onrenamekeydown,
		onrenamecommit,
		ondragoverrow,
		ondragleaverow,
		ondroprow
	}: {
		open: boolean;
		/** null = still loading. */
		rows: SmartlistSummary[] | null;
		error: string | null;
		selectedId: string | null;
		/** False renders every row inert (no smartlist selection wired). */
		selectable: boolean;
		/** id of the row being renamed inline, or null. */
		editingId: string | null;
		editDraft: string;
		renameInputEl?: HTMLInputElement | null;
		/** LIBM-84: id of the row refusing the live track drag, or null. */
		refusedId?: string | null;
		ontoggle: () => void;
		onclickrow: (sl: SmartlistSummary) => void;
		/** SMART (#10024): double-click opens the rule editor modal. */
		ondblclickrow?: (sl: SmartlistSummary) => void;
		/** Every row keydown, for the context menu's keyboard opener. */
		onmenukey: (event: KeyboardEvent, sl: SmartlistSummary) => void;
		oncontextmenurow: (event: MouseEvent, sl: SmartlistSummary) => void;
		oneditdraft: (value: string) => void;
		onrenamekeydown: (event: KeyboardEvent) => void;
		onrenamecommit: () => void;
		ondragoverrow: (event: DragEvent, sl: SmartlistSummary) => void;
		ondragleaverow: (sl: SmartlistSummary) => void;
		ondroprow: () => void;
	} = $props();
</script>

<div
	class="row folder"
	data-testid="smartlists-folder"
	role="button"
	tabindex="0"
	onclick={() => ontoggle()}
	onkeydown={(e) => {
		if (e.key === 'Enter') ontoggle();
	}}
>
	<span class="disclosure" class:open>&#9656;</span>
	<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
		<path d="M1 3h5l1.5 2H15v8H1z" fill="currentColor" />
	</svg>
	<span class="name">Smartlists</span>
</div>
{#if open}
	{#if error !== null}
		<div class="row child rb-inert" title={error}>
			<span class="name error">smartlists unavailable</span>
		</div>
	{:else if rows === null}
		<div class="row child rb-inert">
			<span class="name dim">...</span>
		</div>
	{:else if rows.length === 0}
		<div class="row child rb-inert" data-testid="smartlists-empty">
			<span class="name dim">no smartlists yet</span>
		</div>
	{:else}
		{#each rows as sl (sl.id)}
			<div
				class="row child"
				class:rb-inert={!selectable}
				class:selected={selectedId === sl.id}
				role="button"
				tabindex="0"
				data-testid="smartlist-row"
				data-smartlist-id={sl.id}
				class:drop-refused={refusedId === sl.id}
				title={refusedId === sl.id ? SMARTLIST_DROP_REFUSAL : sl.rule_summary}
				ondragover={(e) => ondragoverrow(e, sl)}
				ondragleave={() => ondragleaverow(sl)}
				ondrop={() => ondroprow()}
				onclick={() => onclickrow(sl)}
				ondblclick={() => {
					if (editingId !== sl.id) ondblclickrow(sl);
				}}
				onkeydown={(e) => {
					if (e.key === 'Enter') onclickrow(sl);
					onmenukey(e, sl);
				}}
				oncontextmenu={(e) => oncontextmenurow(e, sl)}
			>
				<svg class="gear" viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
					<path
						d="M8 5.2A2.8 2.8 0 1 0 8 10.8 2.8 2.8 0 0 0 8 5.2zm0 4.3a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zm6-.5.1-1-1.5-.6-.2-.6.8-1.4-.7-.7-1.4.8-.6-.3L9.9 3.7h-1L8.3 5.2l-.6.3-1.4-.8-.7.7.8 1.4-.3.6-1.5.5v1l1.5.6.3.6-.8 1.4.7.7 1.4-.8.6.3.6 1.5h1l.6-1.5.6-.3 1.4.8.7-.7-.8-1.4.3-.6z"
						fill="currentColor"
					/>
				</svg>
				{#if editingId === sl.id}
					<input
						bind:this={renameInputEl}
						class="rename-input"
						type="text"
						value={editDraft}
						aria-label="Rename smartlist"
						onclick={(e) => e.stopPropagation()}
						onmousedown={(e) => e.stopPropagation()}
						oninput={(e) => oneditdraft(e.currentTarget.value)}
						onkeydown={(e) => onrenamekeydown(e)}
						onblur={() => onrenamecommit()}
					/>
				{:else}
					<span class="name" title={sl.rule_summary}>{sl.name}</span>
				{/if}
				<span class="count">{sl.count ?? '--'}</span>
			</div>
		{/each}
	{/if}
{/if}

<style>
	.row {
		display: flex;
		align-items: center;
		gap: 5px;
		height: 20px;
		padding: 0 6px;
		color: var(--rb-text);
		cursor: pointer;
		white-space: nowrap;
	}
	.row:hover {
		background: var(--rb-panel-raised);
	}
	.row.selected {
		background: var(--rb-select);
	}
	.row.drop-refused {
		cursor: no-drop;
		box-shadow: inset 0 0 0 1px var(--rb-text-dim);
		opacity: 0.6;
	}
	.row svg {
		flex: none;
		color: var(--rb-text-dim);
	}
	.row.child {
		padding-left: 22px;
	}
	.name {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.rename-input {
		flex: 1;
		min-width: 0;
		height: 16px;
		margin: 0;
		padding: 0 2px;
		border: 1px solid var(--rb-accent);
		background: var(--rb-panel);
		color: var(--rb-text);
		font: inherit;
		outline: none;
	}
	.disclosure {
		flex: none;
		display: inline-block;
		width: 8px;
		color: var(--rb-text-dim);
		transition: transform 0.1s;
	}
	.disclosure.open {
		transform: rotate(90deg);
	}
	.row.rb-inert {
		opacity: 0.5;
		cursor: default;
	}
	.row.rb-inert:hover {
		background: transparent;
	}
	.name.dim,
	.name.error {
		color: var(--rb-text-dim);
	}
	.gear {
		flex: none;
	}
	.count {
		flex: none;
		min-width: 4ch;
		align-self: stretch;
		display: flex;
		align-items: center;
		justify-content: flex-end;
		font-variant-numeric: tabular-nums;
		color: var(--rb-text-dim);
	}

	.rename-input:focus-visible {
		/* A11Y-11403: keyboard focus ring paired with this file's outline reset. */
		outline: 2px solid var(--rb-focus-ring);
		outline-offset: 2px;
	}

	/* A11Y-11811: reduced motion (OS or Appearance); mirrors app.css. */
	@media (prefers-reduced-motion: reduce) {
		:where(*) {
			animation-duration: 0.01ms;
			animation-iteration-count: 1;
			transition-duration: 0.01ms;
		}
	}
	:global(html[data-reduce-motion]) :where(*) {
		animation-duration: 0.01ms;
		animation-iteration-count: 1;
		transition-duration: 0.01ms;
	}
</style>
