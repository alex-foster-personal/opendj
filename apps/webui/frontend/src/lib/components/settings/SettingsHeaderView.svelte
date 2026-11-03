<script lang="ts">
	/**
	 * The settings overlay header: back and forward through settings
	 * navigation, the search box and the close button. SettingsOverlay.svelte
	 * owns the history, the query, keyboard routing, and panel drag.
	 */
	interface Props {
		canBack: boolean;
		canForward: boolean;
		onBack: () => void;
		onForward: () => void;
		/** The search input, so the container can focus it on open. */
		searchEl?: HTMLInputElement | null;
		query: string;
		onQueryInput: (event: Event) => void;
		onSearchKeydown: (event: KeyboardEvent) => void;
		onClose: () => void;
		onHeadPointerDown: (event: PointerEvent) => void;
		onHeadPointerMove: (event: PointerEvent) => void;
		onHeadPointerUp: (event: PointerEvent) => void;
	}

	let {
		canBack,
		canForward,
		onBack,
		onForward,
		searchEl = $bindable(null),
		query,
		onQueryInput,
		onSearchKeydown,
		onClose,
		onHeadPointerDown,
		onHeadPointerMove,
		onHeadPointerUp
	}: Props = $props();
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<header
	class="so-head"
	onpointerdown={onHeadPointerDown}
	onpointermove={onHeadPointerMove}
	onpointerup={onHeadPointerUp}
	onpointercancel={onHeadPointerUp}
>
	<span class="so-grip" aria-hidden="true" title="Drag to move">
		<svg width="14" height="18" viewBox="0 0 14 18">
			<path
				d="M2.5 2v14M7 2v14M11.5 2v14"
				fill="none"
				stroke="currentColor"
				stroke-width="1.75"
				stroke-linecap="round"
			/>
		</svg>
	</span>
	<div class="so-nav">
		<button
			type="button"
			class="so-hist"
			disabled={!canBack}
			onclick={() => onBack()}
			title="Back in settings navigation"
			aria-label="Back"
		>
			&lt;
		</button>
		<button
			type="button"
			class="so-hist"
			disabled={!canForward}
			onclick={() => onForward()}
			title="Forward in settings navigation"
			aria-label="Forward"
		>
			&gt;
		</button>
	</div>
	<input
		bind:this={searchEl}
		class="so-search"
		type="search"
		placeholder="Search settings..."
		spellcheck="false"
		autocomplete="off"
		value={query}
		oninput={onQueryInput}
		onkeydown={onSearchKeydown}
		aria-label="Search settings"
	/>
	<button
		type="button"
		class="so-close"
		onclick={() => onClose()}
		aria-label="Close settings"
		title="Close (Esc)"
	>
		Esc
	</button>
</header>

<style>
	.so-head {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 12px 12px 10px;
		border-bottom: 1px solid var(--border, #1c222c);
		cursor: grab;
		touch-action: none;
		user-select: none;
	}
	.so-head:active {
		cursor: grabbing;
	}
	.so-grip {
		display: inline-flex;
		flex: none;
		color: var(--muted, #9aa4b2);
	}
	.so-nav {
		display: inline-flex;
		gap: 2px;
	}
	.so-hist,
	.so-close {
		height: 32px;
		min-width: 32px;
		padding: 0 8px;
		border-radius: 8px;
		border: 1px solid var(--border, #1c222c);
		background: var(--surface, #121720);
		color: var(--fg, #e6e9ef);
		cursor: pointer;
	}
	.so-hist:disabled {
		opacity: 0.35;
		cursor: default;
	}
	.so-search {
		flex: 1;
		height: 40px;
		padding: 0 14px;
		border-radius: 10px;
		border: 1px solid var(--border, #1c222c);
		background: var(--bg, #0b0d11);
		color: var(--fg, #e6e9ef);
		font-size: 1.05rem;
		outline: none;
		cursor: text;
		user-select: text;
		touch-action: auto;
	}
	.so-search:focus {
		border-color: var(--accent, #ffb43a);
	}
</style>
