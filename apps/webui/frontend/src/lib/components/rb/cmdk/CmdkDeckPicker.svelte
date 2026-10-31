<script lang="ts">
	// CMDK-09: Enter on a track opens this picker in place of the results. The
	// master deck is listed first, then the others by number. The live master is
	// never replaced (the browser's load path refuses it too), and replacing any
	// other playing deck asks first in a modal that confirms only with Cmd+Enter
	// or a click; Esc, X or a click outside cancels and plain Enter never confirms.
	// Keys are the bar's: focus stays in the search input the whole time.
	import type { DeckId } from '$lib/rb/deck-id';
	import type { PickerDeck } from '$lib/rb/cmdk-deck-picker';

	let {
		trackTitle,
		query,
		decks,
		selected,
		guard,
		modKey,
		onHover,
		onPick,
		onConfirm,
		onCancel
	}: {
		trackTitle: string;
		/** The search the picker came from, kept so Esc goes back to it. */
		query: string;
		decks: readonly PickerDeck[];
		selected: number;
		/** The playing deck a confirm is being asked for, or null. */
		guard: DeckId | null;
		/** The confirm modifier as this platform names it: ⌘ on macOS, Ctrl elsewhere. */
		modKey: string;
		onHover: (index: number) => void;
		onPick: (index: number) => void;
		onConfirm: () => void;
		onCancel: () => void;
	} = $props();

	const guarded = $derived(guard === null ? null : (decks.find((deck) => deck.id === guard) ?? null));

	function stateLabel(deck: PickerDeck): string {
		if (deck.is_master) return deck.playing ? 'Master · playing' : 'Master';
		if (deck.playing) return 'Playing';
		return deck.loaded ? 'Loaded · paused' : 'Empty';
	}

	function willLabel(deck: PickerDeck): string {
		if (deck.is_master) return 'The live master is never replaced';
		if (deck.playing) return 'Replaces the live track: asks first';
		return deck.loaded ? `Replaces ${deck.title ?? 'the loaded track'}` : 'Loads here';
	}
</script>

<div class="picker" role="listbox" aria-label="Load to deck" data-testid="command-bar-picker">
	<div class="pk-head">
		<span>
			{#if query.trim() !== ''}<span class="crumb" title="Your search is kept; Esc goes back to these results">{query.trim()} ›</span>{/if}
			Load <b>{trackTitle}</b> to
		</span>
		<span>Esc back</span>
	</div>
	{#each decks as deck, i (deck.id)}
		<!-- svelte-ignore a11y_click_events_have_key_events -->
		<div
			class="dk"
			class:sel={i === selected}
			class:master={deck.is_master}
			class:locked={deck.is_master}
			role="option"
			tabindex="-1"
			aria-selected={i === selected}
			aria-disabled={deck.is_master}
			data-testid="command-bar-picker-deck"
			data-deck={deck.id}
			onmousemove={() => {
				if (guard === null && i !== selected) onHover(i);
			}}
			onclick={() => {
				if (guard === null) onPick(i);
			}}
		>
			<span class="dnum" title={`Deck ${deck.id}`}>{deck.id}</span>
			<span class="dinfo">
				{#if deck.loaded}
					<span class="dt">{deck.title ?? '(untitled)'}<small>{deck.artist ?? ''}</small></span>
				{:else}
					<span class="dt empty">Empty deck</span>
				{/if}
				{#if i === selected}<span class="will">{willLabel(deck)}</span>{/if}
			</span>
			<span class="dstate">{#if deck.is_master && deck.playing}<span class="live-dot"></span>{/if}{stateLabel(deck)}</span>
		</div>
	{/each}
</div>

{#if guarded !== null}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="guard" data-testid="command-bar-guard" onclick={onCancel}>
		<!-- svelte-ignore a11y_click_events_have_key_events -->
		<div class="guard-box" role="alertdialog" aria-labelledby="cmdk2-guard-title" tabindex="-1" onclick={(event) => event.stopPropagation()}>
			<button type="button" class="x" aria-label="Cancel" onclick={onCancel}
				><svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true"><path d="M2 2l8 8M10 2l-8 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" /></svg></button
			>
			<!-- A guard asked again after another load describes the deck as it is now (Codex r4276170535). -->
			{#if guarded.playing}
				<h4 id="cmdk2-guard-title">Deck {guarded.id} is playing live</h4>
				<p>Loading here stops "{guarded.title ?? 'the playing track'}" mid-mix.</p>
			{:else if guarded.loaded}
				<h4 id="cmdk2-guard-title">Deck {guarded.id} changed</h4>
				<p>It now holds "{guarded.title ?? 'another track'}", paused. Loading here replaces it.</p>
			{:else}
				<h4 id="cmdk2-guard-title">Deck {guarded.id} changed</h4>
				<p>It is empty now.</p>
			{/if}
			<div class="guard-acts">
				<button type="button" class="danger" data-testid="command-bar-guard-confirm" onclick={onConfirm}>{guarded.loaded ? 'Replace it' : 'Load it'} · {modKey}Enter</button>
				<button type="button" onclick={onCancel}>{guarded.playing ? 'Keep playing' : 'Cancel'} · Esc</button>
			</div>
		</div>
	</div>
{/if}

<style>
	.picker {
		display: grid;
		gap: 3px;
		padding: 6px 4px 8px;
	}
	.pk-head {
		display: flex;
		justify-content: space-between;
		padding: 4px 6px 6px;
		font-size: 11px;
		color: var(--rb-text-dim, #8a94a3);
	}
	.pk-head b {
		color: var(--rb-text, #e6e9ee);
	}
	.crumb {
		margin-right: 6px;
	}
	.dk {
		display: grid;
		grid-template-columns: 24px 1fr auto;
		align-items: center;
		gap: 10px;
		padding: 7px 8px;
		border-radius: 5px;
		cursor: pointer;
	}
	.dk.sel {
		background: color-mix(in srgb, var(--rb-cmdk-pick, #a46ef0) 15%, var(--rb-panel-raised, #1b1f26));
	}
	.dk.locked {
		cursor: not-allowed;
		opacity: 0.75;
	}
	.dnum {
		display: grid;
		place-items: center;
		width: 22px;
		height: 22px;
		border-radius: 4px;
		border: 1px solid var(--rb-border, #2c323c);
		font-weight: 700;
		font-size: 12px;
	}
	.master .dnum {
		border-color: var(--rb-master, #f0a020);
		color: var(--rb-master, #f0a020);
	}
	.dinfo {
		display: grid;
		min-width: 0;
		gap: 2px;
	}
	.dt {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 13px;
	}
	.dt small {
		margin-left: 6px;
		font-size: 11px;
		color: var(--rb-text-dim, #8a94a3);
	}
	.dt.empty {
		color: var(--rb-text-dim, #8a94a3);
	}
	.will {
		font-size: 11px;
		color: var(--rb-text-dim, #8a94a3);
	}
	.dstate {
		font-size: 11px;
		color: var(--rb-text-dim, #8a94a3);
		white-space: nowrap;
	}
	.live-dot {
		display: inline-block;
		width: 6px;
		height: 6px;
		margin-right: 5px;
		border-radius: 50%;
		background: var(--rb-red, #ff6b6b);
	}
	.guard {
		position: fixed;
		inset: 0;
		z-index: 950;
		display: grid;
		place-items: center;
		background: rgb(0 0 0 / 35%);
	}
	.guard-box {
		position: relative;
		width: min(420px, calc(100vw - 32px));
		padding: 16px 18px;
		border-radius: 8px;
		background: var(--rb-panel-raised, #1b1f26);
		border: 1px solid var(--rb-border, #2c323c);
		color: var(--rb-text, #e6e9ee);
		box-shadow: 0 18px 50px rgb(0 0 0 / 45%);
	}
	.guard-box h4 {
		margin: 0 0 8px;
		font-size: 14px;
	}
	.guard-box p {
		margin: 0 0 14px;
		font-size: 12px;
		color: var(--rb-text-dim, #8a94a3);
	}
	.x {
		position: absolute;
		top: 8px;
		right: 10px;
		background: none;
		border: 0;
		color: inherit;
		font-size: 16px;
		cursor: pointer;
	}
	.guard-acts {
		display: flex;
		gap: 8px;
	}
	.guard-acts button {
		padding: 6px 10px;
		border-radius: 5px;
		border: 1px solid var(--rb-border, #2c323c);
		background: transparent;
		color: inherit;
		font-size: 12px;
		cursor: pointer;
	}
	.guard-acts .danger {
		border-color: var(--rb-red, #ff6b6b);
		color: var(--rb-red, #ff6b6b);
	}
</style>
