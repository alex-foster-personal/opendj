<script lang="ts">
	/**
	 * Props-only half of the LIBUX-04 hotkeys overlay. Two views behind a tab
	 * control (grid default, searchable list). Header tab order: search, Grid,
	 * List, close. Live type-to-filter with yellow
	 * match highlighting in both views. X button always hides; Esc is handled
	 * globally by installHotkeysOverlayHotkeys. HotkeysOverlay.svelte reads the
	 * overlay store and passes its state and setters in.
	 */
	import { tick } from 'svelte';
	import { focusTrap } from '$lib/keyboard/focus-trap';
	import {
		filterHotkeys,
		shortcutCountAnnouncement,
		visibleShortcutCount,
		type FilteredHotkey,
		type HighlightPiece,
		type HotkeyEntry,
		type HotkeysOverlayView
	} from './hotkeys-filter';
	import type { HotkeysOverlayTrigger } from './hotkeys-registry';

	const {
		open,
		view,
		query,
		entries,
		trigger,
		onHide,
		onSetView,
		onSetQuery
	}: {
		open: boolean;
		view: HotkeysOverlayView;
		query: string;
		/** Every registered binding (HOTKEY_REGISTRY in the app). */
		entries: readonly HotkeyEntry[];
		/** The X button's name and shortcuts, from the live toggle key (#12277). */
		trigger: HotkeysOverlayTrigger;
		onHide: () => void;
		onSetView: (next: HotkeysOverlayView) => void;
		onSetQuery: (next: string) => void;
	} = $props();

	let searchEl = $state<HTMLInputElement | null>(null);

	const filtered = $derived(filterHotkeys(entries, query));
	// #12288: one polite summary of the result; the rows themselves are never read out twice.
	const resultSummary = $derived(shortcutCountAnnouncement(visibleShortcutCount(filtered)));
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
		return order.map((name) => {
			const rows = map.get(name);
			if (rows === undefined) throw new Error(`hotkey group "${name}" is in order but not in map`);
			return { name, rows };
		});
	});

	$effect(() => {
		if (!open) return;
		void tick().then(() => searchEl?.focus());
	});

	function onBackdrop(e: MouseEvent): void {
		if (e.target === e.currentTarget) onHide();
	}

	function onQueryInput(e: Event): void {
		const el = e.currentTarget;
		if (!(el instanceof HTMLInputElement)) return;
		onSetQuery(el.value);
	}
</script>

{#snippet pieces(parts: HighlightPiece[])}
	{#each parts as piece, i (i)}
		{#if piece.matched}<mark class="hk-mark">{piece.text}</mark>{:else}{piece.text}{/if}
	{/each}
{/snippet}

{#if open}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="hk-backdrop" role="presentation" onclick={onBackdrop}>
		<div
			class="hk-panel"
			role="dialog"
			aria-modal="true"
			aria-label="Hotkeys"
			tabindex="-1"
			use:focusTrap={{ initialFocus: '.hk-search' }}
			onclick={(e) => e.stopPropagation()}
		>
			<header class="hk-head">
				<input
					bind:this={searchEl}
					class="hk-search"
					type="search"
					placeholder="Filter hotkeys..."
					spellcheck="false"
					autocomplete="off"
					data-hotkeys-overlay-search="true"
					value={query}
					oninput={onQueryInput}
					aria-label="Filter hotkeys"
				/>
				<div class="hk-tabs" aria-label="Hotkeys view">
					<button
						type="button"
						class="hk-tab"
						class:on={view === 'grid'}
						onclick={() => onSetView('grid')}
					>
						Grid
					</button>
					<button
						type="button"
						class="hk-tab"
						class:on={view === 'list'}
						onclick={() => onSetView('list')}
					>
						List
					</button>
				</div>
				<button
					type="button"
					class="hk-close"
					onclick={() => onHide()}
					aria-label={trigger.label}
					aria-keyshortcuts={trigger.ariaKeyshortcuts}
					title={trigger.title}
				>
					X
				</button>
			</header>

			<p class="hk-sr-only" role="status" aria-live="polite" data-hotkeys-overlay-count="true">
				{resultSummary}
			</p>

			<div class="hk-body">
				{#if entries.length === 0}
					<p class="hk-empty">no bindings registered</p>
				{:else if filtered.length === 0}
					<p class="hk-empty">No hotkeys match.</p>
				{:else if view === 'list'}
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
	/* The browser's default placeholder grey is 4.2:1 here; the muted token clears AA in every skin. */
	.hk-search::placeholder {
		color: var(--muted, #9aa4b2);
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

	.hk-sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0, 0, 0, 0);
		white-space: nowrap;
		border: 0;
	}

	.hk-search:focus-visible {
		/* A11Y-11403: keyboard focus ring paired with this file's outline reset. */
		outline: 2px solid var(--rb-focus-ring);
		outline-offset: 2px;
	}
</style>
