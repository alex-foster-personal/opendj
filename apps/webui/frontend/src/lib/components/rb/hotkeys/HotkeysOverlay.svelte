<script lang="ts">
	/**
	 * LIBUX-04 hotkeys overlay. Two views behind a tab control (grid default,
	 * searchable list). Live type-to-filter with yellow match highlighting
	 * in both views. X button always hides; Esc is handled globally by
	 * installHotkeysOverlayHotkeys.
	 */
	import { tick } from 'svelte';
	import { filterHotkeys, type FilteredHotkey, type HighlightPiece } from './hotkeys-filter';
	import { HOTKEY_REGISTRY } from './hotkeys-registry';
	import {
		hideHotkeysOverlay,
		hotkeysOverlay,
		setHotkeysOverlayQuery,
		setHotkeysOverlayView
	} from './hotkeys-overlay.svelte';

	let searchEl = $state<HTMLInputElement | null>(null);

	const filtered = $derived(filterHotkeys(HOTKEY_REGISTRY, hotkeysOverlay.query));
	const grouped = $derived.by(() => {
		const order: string[] = [];
		const map = new Map<string, FilteredHotkey[]>();
		for (const row of filtered) {
			const name = row.entry.group;
			const bucket = map.get(name);
			if (bucket === undefined) {
				map.set(name, [row]);
				order.push(name);
			} else {
				bucket.push(row);
			}
		}
		return order.map((name) => ({ name, rows: map.get(name) ?? [] }));
	});

	$effect(() => {
		if (!hotkeysOverlay.open) return;
		void tick().then(() => searchEl?.focus());
	});

	function onBackdrop(e: MouseEvent): void {
		if (e.target === e.currentTarget) hideHotkeysOverlay();
	}

	function onQueryInput(e: Event): void {
		const el = e.currentTarget;
		if (!(el instanceof HTMLInputElement)) return;
		setHotkeysOverlayQuery(el.value);
	}
</script>

{#snippet pieces(parts: HighlightPiece[])}
	{#each parts as piece, i (i)}
		{#if piece.matched}<mark class="hk-mark">{piece.text}</mark>{:else}{piece.text}{/if}
	{/each}
{/snippet}

{#if hotkeysOverlay.open}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="hk-backdrop" role="presentation" onclick={onBackdrop}>
		<div
			class="hk-panel"
			role="dialog"
			aria-modal="true"
			aria-label="Hotkeys"
			tabindex="-1"
			onclick={(e) => e.stopPropagation()}
		>
			<header class="hk-head">
				<div class="hk-tabs" aria-label="Hotkeys view">
					<button
						type="button"
						class="hk-tab"
						class:on={hotkeysOverlay.view === 'grid'}
						onclick={() => setHotkeysOverlayView('grid')}
					>
						Grid
					</button>
					<button
						type="button"
						class="hk-tab"
						class:on={hotkeysOverlay.view === 'list'}
						onclick={() => setHotkeysOverlayView('list')}
					>
						List
					</button>
				</div>
				<input
					bind:this={searchEl}
					class="hk-search"
					type="search"
					placeholder="Filter hotkeys..."
					spellcheck="false"
					autocomplete="off"
					data-hotkeys-overlay-search="true"
					value={hotkeysOverlay.query}
					oninput={onQueryInput}
					aria-label="Filter hotkeys"
				/>
				<button
					type="button"
					class="hk-close"
					onclick={() => hideHotkeysOverlay()}
					aria-label="Close hotkeys overlay"
					title="Hide hotkeys overlay (Esc)"
				>
					X
				</button>
			</header>

			<div class="hk-body">
				{#if HOTKEY_REGISTRY.length === 0}
					<p class="hk-empty">no bindings registered</p>
				{:else if filtered.length === 0}
					<p class="hk-empty">No hotkeys match.</p>
				{:else if hotkeysOverlay.view === 'list'}
					<ul class="hk-list">
						{#each filtered as row (row.entry.id)}
							<li class="hk-row">
								<kbd class="hk-chord">{@render pieces(row.chordPieces)}</kbd>
								<span class="hk-desc">{@render pieces(row.descriptionPieces)}</span>
								<span class="hk-group">{@render pieces(row.groupPieces)}</span>
							</li>
						{/each}
					</ul>
				{:else}
					<div class="hk-grid">
						{#each grouped as section (section.name)}
							<section class="hk-section">
								<h2 class="hk-section-title">{section.name}</h2>
								<ul class="hk-cards">
									{#each section.rows as row (row.entry.id)}
										<li class="hk-card">
											<kbd class="hk-chord">{@render pieces(row.chordPieces)}</kbd>
											<span class="hk-desc">{@render pieces(row.descriptionPieces)}</span>
										</li>
									{/each}
								</ul>
							</section>
						{/each}
					</div>
				{/if}
			</div>
		</div>
	</div>
{/if}

<style>
	.hk-backdrop {
		position: fixed;
		inset: 0;
		z-index: 410;
		display: flex;
		align-items: flex-start;
		justify-content: center;
		padding: 10vh 1rem 2rem;
		background: var(--overlay, rgba(0, 0, 0, 0.72));
	}
	.hk-panel {
		width: min(780px, 96vw);
		max-height: 80vh;
		display: flex;
		flex-direction: column;
		background: var(--surface-raised, #222a38);
		border: 1px solid var(--border, #1c222c);
		border-radius: 12px;
		box-shadow: 0 18px 50px rgba(0, 0, 0, 0.45);
		overflow: hidden;
	}
	.hk-head {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 12px 12px 10px;
		border-bottom: 1px solid var(--border, #1c222c);
	}
	.hk-tabs {
		display: inline-flex;
		gap: 2px;
	}
	.hk-tab,
	.hk-close {
		height: 32px;
		min-width: 32px;
		padding: 0 10px;
		border-radius: 8px;
		border: 1px solid var(--border, #1c222c);
		background: var(--surface, #121720);
		color: var(--fg, #e6e9ef);
		cursor: pointer;
		font: inherit;
	}
	.hk-tab.on {
		border-color: var(--accent, #ffb43a);
		color: var(--accent, #ffb43a);
	}
	.hk-search {
		flex: 1;
		height: 40px;
		padding: 0 14px;
		border-radius: 10px;
		border: 1px solid var(--border, #1c222c);
		background: var(--bg, #0b0d11);
		color: var(--fg, #e6e9ef);
		font-size: 1.05rem;
		outline: none;
	}
	.hk-search:focus {
		border-color: var(--accent, #ffb43a);
	}
	.hk-body {
		min-height: 0;
		flex: 1;
		overflow: auto;
		padding: 12px;
	}
	.hk-empty {
		margin: 1.5rem 0;
		text-align: center;
		color: var(--muted, #9aa4b2);
	}
	.hk-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.hk-row {
		display: grid;
		grid-template-columns: 8.5rem 1fr auto;
		gap: 12px;
		align-items: baseline;
		padding: 8px 10px;
		border-radius: 8px;
	}
	.hk-row:hover {
		background: var(--surface, #121720);
	}
	.hk-grid {
		display: flex;
		flex-direction: column;
		gap: 16px;
	}
	.hk-section-title {
		margin: 0 0 8px;
		font-size: 0.75rem;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		color: var(--muted, #9aa4b2);
	}
	.hk-cards {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
		gap: 8px;
	}
	.hk-card {
		display: flex;
		flex-direction: column;
		gap: 6px;
		padding: 10px 12px;
		border: 1px solid var(--border, #1c222c);
		border-radius: 8px;
		background: var(--surface, #121720);
	}
	.hk-chord {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.85rem;
		padding: 2px 6px;
		border-radius: 4px;
		border: 1px solid var(--border, #1c222c);
		background: var(--bg, #0b0d11);
		width: fit-content;
	}
	.hk-desc {
		color: var(--fg, #e6e9ef);
		font-size: 0.9rem;
	}
	.hk-group {
		color: var(--muted, #9aa4b2);
		font-size: 0.75rem;
		white-space: nowrap;
	}
	.hk-mark {
		background: #ffe566;
		color: #111;
		padding: 0;
		border-radius: 2px;
	}
</style>
