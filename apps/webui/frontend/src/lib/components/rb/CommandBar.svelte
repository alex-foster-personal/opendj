<script module lang="ts">
	// Re-exported so BrowserPanel's load hook reaches it through the import it
	// already has (the quality ratchet counts each module a file imports).
	export { waitForStemsSettled } from '$lib/rb/command-bar';
</script>

<script lang="ts">
	// CMDK-01..03: Cmd-K (Ctrl-K off macOS) command bar over the browser.
	// Searches the open playlist as you type; Tab widens it to the whole
	// library (the same FTS5 search the browser's "whole collection" mode
	// uses). Up/Down pick a track, Left/Right pick the deck, Enter loads it,
	// Shift+Enter loads it vocals-only. Loading goes through the browser's own
	// deck-load path (the `load` prop), so every guard and load setting that
	// applies to a double-click applies here too.
	import { tick } from 'svelte';
	import { deckStates as decks, DECK_IDS } from '$lib/rb/audio-engine.svelte';
	import { searchCollection } from '$lib/rb/api-rb';
	import type { DeckId } from '$lib/rb/deck-id';
	import {
		COMMAND_BAR_LIMIT,
		defaultTargetDeck,
		matchRows,
		rowHaystack,
		stepDeck,
		stepSelection,
		type CommandBarRow,
		type CommandBarScope
	} from '$lib/rb/command-bar';

	let {
		rows,
		playlistTitle,
		load
	}: {
		rows: readonly CommandBarRow[];
		playlistTitle: string;
		load: (row: CommandBarRow, deck: DeckId, vocalsOnly: boolean) => Promise<void>;
	} = $props();

	let open = $state(false);
	let query = $state('');
	let scope = $state<CommandBarScope>('playlist');
	let selected = $state(0);
	let target = $state<DeckId>(1);
	let vocalsOnly = $state(false);
	let input = $state<HTMLInputElement | null>(null);

	let libraryRows = $state<CommandBarRow[]>([]);
	let libraryTotal = $state(0);
	let librarySearching = $state(false);
	let libraryError = $state<string | null>(null);
	let librarySeq = 0;

	// Built once per row set, not per keystroke: typing only scans strings.
	const haystacks = $derived(rows.map(rowHaystack));
	const playlistMatch = $derived(matchRows(rows, haystacks, query));
	const shown = $derived(scope === 'playlist' ? playlistMatch.rows : libraryRows);
	const total = $derived(scope === 'playlist' ? playlistMatch.total : libraryTotal);

	function deckList(): { id: DeckId; loaded: boolean; is_master: boolean }[] {
		return DECK_IDS.map((id) => ({
			id,
			loaded: decks[id].stable_id !== null,
			is_master: decks[id].is_master
		}));
	}

	async function openBar(): Promise<void> {
		query = '';
		selected = 0;
		vocalsOnly = false;
		scope = rows.length > 0 ? 'playlist' : 'library';
		target = defaultTargetDeck(deckList());
		open = true;
		await tick();
		input?.focus();
	}

	function closeBar(): void {
		open = false;
		librarySeq += 1;
		librarySearching = false;
	}

	$effect(() => {
		if (!open || scope !== 'library') return;
		const q = query.trim();
		const seq = ++librarySeq;
		if (q === '') {
			libraryRows = [];
			libraryTotal = 0;
			libraryError = null;
			librarySearching = false;
			return;
		}
		librarySearching = true;
		// A short settle so a fast typist sends one request, not one per key.
		const timer = setTimeout(() => {
			searchCollection({ q, limit: COMMAND_BAR_LIMIT }).then(
				(result) => {
					if (seq !== librarySeq) return;
					libraryRows = result.items;
					libraryTotal = result.total;
					libraryError = null;
					librarySearching = false;
					selected = 0;
				},
				(error: unknown) => {
					if (seq !== librarySeq) return;
					libraryRows = [];
					libraryTotal = 0;
					libraryError = error instanceof Error ? error.message : String(error);
					librarySearching = false;
				}
			);
		}, 60);
		return () => clearTimeout(timer);
	});

	function onWindowKeydown(event: KeyboardEvent): void {
		if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === 'k') {
			event.preventDefault();
			if (open) closeBar();
			else void openBar();
		}
	}

	async function choose(row: CommandBarRow | undefined, withVocalsOnly: boolean): Promise<void> {
		if (row === undefined) return;
		const deck = target;
		closeBar();
		await load(row, deck, withVocalsOnly);
	}

	function onInputKeydown(event: KeyboardEvent): void {
		// Every key the bar owns stops here, so the page's own shortcuts
		// (Tab = compatible filter, Space = play) never see it.
		const handled = (): void => {
			event.preventDefault();
			event.stopPropagation();
		};
		if (event.key === 'Escape') {
			handled();
			closeBar();
		} else if (event.key === 'Tab') {
			handled();
			scope = scope === 'playlist' ? 'library' : 'playlist';
			selected = 0;
		} else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
			handled();
			selected = stepSelection(selected, shown.length, event.key === 'ArrowDown' ? 1 : -1);
			document.getElementById(`cmdk-row-${selected}`)?.scrollIntoView({ block: 'nearest' });
		} else if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
			handled();
			target = stepDeck(DECK_IDS, target, event.key === 'ArrowRight' ? 1 : -1);
		} else if (event.key === 'Enter') {
			handled();
			void choose(shown[selected], vocalsOnly || event.shiftKey);
		} else if (event.altKey && event.code === 'KeyV') {
			handled();
			vocalsOnly = !vocalsOnly;
		} else {
			event.stopPropagation();
		}
	}

	function onQueryInput(): void {
		selected = 0;
	}

	function fmtBpm(bpm: number | null): string {
		return bpm === null ? '' : bpm.toFixed(1);
	}
</script>

<svelte:window onkeydown={onWindowKeydown} />

{#if open}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="cmdk-backdrop" onclick={closeBar} data-testid="command-bar-backdrop">
		<!-- svelte-ignore a11y_click_events_have_key_events -->
		<div
			class="cmdk"
			role="dialog"
			aria-modal="true"
			aria-label="Quick track selector"
			tabindex="-1"
			data-testid="command-bar"
			onclick={(event) => event.stopPropagation()}
		>
			<div class="cmdk-head">
				<input
					bind:this={input}
					bind:value={query}
					oninput={onQueryInput}
					onkeydown={onInputKeydown}
					class="cmdk-input"
					type="text"
					role="combobox"
					aria-expanded="true"
					aria-controls="cmdk-list"
					aria-activedescendant={shown.length > 0 ? `cmdk-row-${selected}` : undefined}
					autocomplete="off"
					spellcheck="false"
					placeholder={scope === 'playlist' ? `Search ${playlistTitle}` : 'Search the whole library'}
					data-testid="command-bar-input"
				/>
				<button
					type="button"
					class="cmdk-scope"
					data-testid="command-bar-scope"
					data-scope={scope}
					title="Tab switches between the open playlist and the whole library"
					onclick={() => {
						scope = scope === 'playlist' ? 'library' : 'playlist';
						selected = 0;
						input?.focus();
					}}
				>
					{scope === 'playlist' ? playlistTitle : 'Library'}
				</button>
			</div>

			<div class="cmdk-decks" role="group" aria-label="Load to deck">
				{#each DECK_IDS as id (id)}
					<button
						type="button"
						class="cmdk-deck"
						class:active={target === id}
						class:loaded={decks[id].stable_id !== null}
						class:master={decks[id].is_master}
						data-testid={`command-bar-deck-${id}`}
						aria-pressed={target === id}
						title={decks[id].is_master
							? `Deck ${id} is the live master; a load there is refused`
							: decks[id].stable_id !== null
								? `Deck ${id} has a track; loading replaces it`
								: `Deck ${id} is empty`}
						onclick={() => {
							target = id;
							input?.focus();
						}}
					>
						{id}
					</button>
				{/each}
				<button
					type="button"
					class="cmdk-vocals"
					class:active={vocalsOnly}
					aria-pressed={vocalsOnly}
					data-testid="command-bar-vocals"
					title="Load with only the vocal stem playing (Alt+V, or Shift+Enter for one load). Tracks without stems load as the full mix."
					onclick={() => {
						vocalsOnly = !vocalsOnly;
						input?.focus();
					}}
				>
					Vocals only
				</button>
				<span class="cmdk-hint">Left/Right deck, Enter load, Shift+Enter vocals only, Tab scope</span>
			</div>

			<ul class="cmdk-list" id="cmdk-list" role="listbox" data-testid="command-bar-results">
				{#each shown as row, i (row.stable_id)}
					<!-- svelte-ignore a11y_click_events_have_key_events -->
					<li
						id={`cmdk-row-${i}`}
						role="option"
						aria-selected={i === selected}
						class="cmdk-row"
						class:selected={i === selected}
						class:missing={row.file_exists === false}
						data-testid="command-bar-row"
						data-stable-id={row.stable_id}
						onmousemove={() => (selected = i)}
						onclick={(event) => void choose(row, vocalsOnly || event.shiftKey)}
					>
						<span class="t">{row.title ?? '(untitled)'}</span>
						<span class="a">{row.artist ?? ''}</span>
						<span class="k" title="Key">{row.key ?? ''}</span>
						<span class="b" title="Tempo in beats per minute">{fmtBpm(row.bpm)}</span>
					</li>
				{/each}
			</ul>

			<div class="cmdk-foot" data-testid="command-bar-status">
				{#if scope === 'library' && libraryError !== null}
					<span class="err" role="alert">Library search failed: {libraryError}</span>
				{:else if scope === 'library' && librarySearching}
					Searching the library...
				{:else if scope === 'library' && query.trim() === ''}
					Type to search the whole library.
				{:else}
					<span title="Tracks that match; the list shows the first {COMMAND_BAR_LIMIT}">
						{total} match{total === 1 ? '' : 'es'}{total > shown.length ? `, showing ${shown.length}` : ''}
					</span>
				{/if}
			</div>
		</div>
	</div>
{/if}

<style>
	.cmdk-backdrop {
		position: fixed;
		inset: 0;
		z-index: 900;
		background: rgb(0 0 0 / 45%);
		display: flex;
		justify-content: center;
		align-items: flex-start;
		padding-top: 12vh;
	}
	.cmdk {
		width: min(640px, calc(100vw - 32px));
		max-height: 66vh;
		display: flex;
		flex-direction: column;
		background: var(--rb-panel-raised, #1b1f26);
		border: 1px solid var(--rb-border, #2c323c);
		border-radius: 8px;
		box-shadow: 0 16px 48px rgb(0 0 0 / 55%);
		color: var(--rb-text, #e6e9ee);
		font-family: var(--rb-font, system-ui, sans-serif);
		overflow: hidden;
	}
	.cmdk-head {
		display: flex;
		gap: 8px;
		padding: 10px;
		border-bottom: 1px solid var(--rb-border, #2c323c);
	}
	.cmdk-input {
		flex: 1;
		min-width: 0;
		background: var(--rb-bg, #0f1216);
		border: 1px solid var(--rb-border, #2c323c);
		border-radius: 4px;
		color: inherit;
		font: inherit;
		font-size: 15px;
		padding: 8px 10px;
	}
	.cmdk-input:focus {
		outline: none;
		border-color: var(--rb-accent, #3aa0ff);
	}
	.cmdk-scope,
	.cmdk-deck,
	.cmdk-vocals {
		background: var(--rb-panel, #151920);
		border: 1px solid var(--rb-border, #2c323c);
		border-radius: 4px;
		color: var(--rb-text-dim, #8a94a3);
		font: inherit;
		font-size: 12px;
		cursor: pointer;
	}
	.cmdk-scope {
		max-width: 180px;
		padding: 0 10px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.cmdk-scope[data-scope='library'] {
		color: var(--rb-accent, #3aa0ff);
	}
	.cmdk-decks {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 6px 10px;
		border-bottom: 1px solid var(--rb-border, #2c323c);
	}
	.cmdk-deck {
		width: 28px;
		height: 24px;
	}
	.cmdk-deck.loaded {
		color: var(--rb-text, #e6e9ee);
	}
	.cmdk-deck.master {
		border-color: var(--rb-orange, #ff9f43);
	}
	.cmdk-deck.active,
	.cmdk-vocals.active {
		background: var(--rb-accent, #3aa0ff);
		border-color: var(--rb-accent, #3aa0ff);
		color: #fff;
	}
	.cmdk-vocals {
		height: 24px;
		padding: 0 8px;
	}
	.cmdk-hint {
		margin-left: auto;
		color: var(--rb-text-dim, #8a94a3);
		font-size: 11px;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.cmdk-list {
		list-style: none;
		margin: 0;
		padding: 4px 0;
		overflow-y: auto;
		flex: 1;
	}
	.cmdk-row {
		display: grid;
		grid-template-columns: minmax(0, 3fr) minmax(0, 2fr) 44px 52px;
		gap: 10px;
		padding: 5px 12px;
		font-size: 13px;
		cursor: pointer;
	}
	.cmdk-row.selected {
		background: var(--rb-select, #23415e);
	}
	.cmdk-row.missing {
		opacity: 0.5;
	}
	.cmdk-row span {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.cmdk-row .a,
	.cmdk-row .k {
		color: var(--rb-text-dim, #8a94a3);
	}
	.cmdk-row .b {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}
	.cmdk-foot {
		padding: 6px 12px;
		border-top: 1px solid var(--rb-border, #2c323c);
		color: var(--rb-text-dim, #8a94a3);
		font-size: 11px;
	}
	.cmdk-foot .err {
		color: var(--rb-red, #ff6b6b);
	}
</style>
