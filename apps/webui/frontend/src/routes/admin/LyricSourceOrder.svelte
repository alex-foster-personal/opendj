<script lang="ts">
	/**
	 * Ordered list of lyric sources with drag-to-reorder (PaneTabs is the
	 * house DnD pattern: draggable rows, drop-target outline, reorder on drop)
	 * plus per-row up/down buttons so the order is also keyboard-operable.
	 *
	 * Save persists via PUT /api/v1/lyrics/config; a 422 (e.g. a source
	 * missing from the list) is surfaced verbatim. The order matters: it is
	 * what the pipeline's next screen/fetch run walks, top source first.
	 */
	import { onMount } from 'svelte';
	import type { LyricsConfig } from '$lib/api';
	import { fetchLyricsConfig, saveLyricsConfig } from './lyrics-api';

	let config = $state<LyricsConfig | null>(null);
	let order = $state<string[]>([]);
	let loadError = $state<string | null>(null);
	let saveError = $state<string | null>(null);
	let saving = $state(false);
	let savedAt = $state<string | null>(null);

	/** Index of the row being dragged; null when no drag is in flight. */
	let dragFrom = $state<number | null>(null);
	/** Index the pointer is currently over, for the drop-target outline. */
	let dragOver = $state<number | null>(null);

	const dirty = $derived(
		config !== null && JSON.stringify(order) !== JSON.stringify(config.source_order)
	);

	onMount(async () => {
		try {
			config = await fetchLyricsConfig();
			order = [...config.source_order];
		} catch (exc) {
			loadError = exc instanceof Error ? exc.message : String(exc);
		}
	});

	function _reorder(from: number, to: number): void {
		const next = [...order];
		const [moved] = next.splice(from, 1);
		next.splice(to, 0, moved);
		order = next;
		savedAt = null;
	}

	function _onDragStart(event: DragEvent, index: number): void {
		dragFrom = index;
		event.dataTransfer?.setData('text/plain', String(index));
		if (event.dataTransfer !== null) event.dataTransfer.effectAllowed = 'move';
	}

	function _onDragOver(event: DragEvent, index: number): void {
		if (dragFrom === null) return;
		event.preventDefault();
		dragOver = index;
	}

	function _onDrop(event: DragEvent, index: number): void {
		event.preventDefault();
		dragOver = null;
		const from = dragFrom;
		dragFrom = null;
		if (from === null || from === index) return;
		_reorder(from, index);
	}

	function _onDragEnd(): void {
		dragFrom = null;
		dragOver = null;
	}

	async function _save(): Promise<void> {
		saving = true;
		saveError = null;
		try {
			config = await saveLyricsConfig(order);
			order = [...config.source_order];
			savedAt = new Date().toISOString();
		} catch (exc) {
			saveError = exc instanceof Error ? exc.message : String(exc);
		} finally {
			saving = false;
		}
	}

	function _reset(): void {
		if (config === null) return;
		order = [...config.source_order];
		saveError = null;
	}
</script>

<div class="source-order">
	<h4>
		Lyric source order
		{#if config?.is_default && !dirty}
			<!-- Hidden while dirty: an edited list no longer matches the
			     default, and asserting it does next to a hot Save button is
			     a lie (visual eval A2, Mon 31 Aug 2026). -->
			<span
				class="badge"
				title="No operator has persisted a custom order yet; this is the shipped default ranking."
			>
				default order
			</span>
		{/if}
	</h4>
	<p class="copy">
		Strongest source first. This order drives the pipeline's next screen/fetch run: for each track
		it tries the top source, then walks down until one yields lyrics. Drag a row (or use the
		arrows) to reorder, then Save.
	</p>

	{#if loadError}
		<div class="error">
			LOAD FAILED

			{loadError}
		</div>
	{:else if config === null}
		<p class="copy">Loading source order...</p>
	{:else}
		<ol class="rows">
			{#each order as source, i (source)}
				<li
					class="row"
					class:drop-target={dragOver === i}
					class:dragging={dragFrom === i}
					draggable="true"
					title={config.source_titles[source] ??
						'no description registered for this source key'}
					ondragstart={(e) => _onDragStart(e, i)}
					ondragover={(e) => _onDragOver(e, i)}
					ondrop={(e) => _onDrop(e, i)}
					ondragend={_onDragEnd}
				>
					<span class="rank" title="Rank in the pipeline's walk order; 1 is tried first.">
						{i + 1}
					</span>
					<span class="grip" aria-hidden="true">::</span>
					<span class="name">{source}</span>
					<span class="steppers">
						<button
							type="button"
							disabled={i === 0}
							title={i === 0 ? 'already first' : 'move this source up one rank'}
							aria-label="move {source} up"
							onclick={() => _reorder(i, i - 1)}
						>
							&#9650;
						</button>
						<button
							type="button"
							disabled={i === order.length - 1}
							title={i === order.length - 1 ? 'already last' : 'move this source down one rank'}
							aria-label="move {source} down"
							onclick={() => _reorder(i, i + 1)}
						>
							&#9660;
						</button>
					</span>
				</li>
			{/each}
		</ol>

		<div class="actions">
			<button
				type="button"
				class="save"
				disabled={!dirty || saving}
				title={dirty
					? 'persist this order via PUT /api/v1/lyrics/config; the next pipeline run uses it'
					: 'nothing to save - the list matches the persisted order'}
				onclick={_save}
			>
				{saving ? 'Saving...' : 'Save order'}
			</button>
			<button
				type="button"
				disabled={!dirty || saving}
				title={dirty ? 'discard the unsaved reordering' : 'nothing to discard'}
				onclick={_reset}
			>
				Reset
			</button>
			{#if savedAt !== null}
				<span class="saved" title="Order persisted at {savedAt} (UTC).">saved</span>
			{/if}
		</div>

		{#if saveError}
			<div class="error">
				SAVE REJECTED

				{saveError}
			</div>
		{/if}
	{/if}
</div>

<style>
	h4 {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		font-size: 0.9rem;
		color: var(--fg);
		margin: 0 0 0.25rem 0;
	}
	.badge {
		font-size: 0.62rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		border: 1px solid var(--border);
		border-radius: 3px;
		padding: 0 0.3rem;
		color: var(--muted);
		cursor: help;
	}
	.copy {
		color: var(--muted);
		font-size: 0.8rem;
		margin: 0 0 0.6rem 0;
		max-width: 80ch;
	}
	.rows {
		list-style: none;
		margin: 0 0 0.6rem 0;
		padding: 0;
		max-width: 34rem;
	}
	.row {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.35rem 0.6rem;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--surface);
		margin-bottom: 0.25rem;
		cursor: grab;
	}
	.row.dragging {
		opacity: 0.5;
	}
	.row.drop-target {
		box-shadow: inset 0 2px 0 0 var(--accent);
	}
	.rank {
		font-variant-numeric: tabular-nums;
		color: var(--muted);
		font-size: 0.72rem;
		width: 1.2rem;
		text-align: right;
		cursor: help;
	}
	.grip {
		color: var(--muted);
		font-size: 0.8rem;
		letter-spacing: -0.05em;
	}
	.name {
		font-size: 0.82rem;
		color: var(--fg);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.steppers {
		margin-left: auto;
		display: inline-flex;
		gap: 0.2rem;
	}
	.steppers button {
		background: var(--chip-bg);
		color: var(--fg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 0 0.35rem;
		font-size: 0.6rem;
		cursor: pointer;
	}
	.steppers button:disabled {
		opacity: 0.35;
		cursor: default;
	}
	.actions {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}
	.actions button {
		background: var(--chip-bg);
		color: var(--fg);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 0.25rem 0.7rem;
		font-size: 0.78rem;
		cursor: pointer;
	}
	.actions button.save:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}
	.actions button:disabled {
		opacity: 0.45;
		cursor: default;
	}
	.saved {
		color: var(--kpi-ok);
		font-size: 0.75rem;
		cursor: help;
	}
	.error {
		background: var(--danger);
		color: #fff;
		padding: 0.6rem 0.8rem;
		border-radius: 6px;
		font-size: 0.8rem;
		font-weight: 600;
		white-space: pre-wrap;
		line-height: 1.45;
		margin-top: 0.5rem;
		max-width: 40rem;
	}
</style>
