<script lang="ts">
	// Browser pane search (SCREENSHOT-SPEC 5c). Modes:
	//   filter     - Cmd+F: client filter within this track list
	//   find       - Cmd+F again: in-place highlight (no filter), ≥3 chars
	//   collection - Cmd+Shift+F: whole-collection FTS5
	let {
		value,
		mode = 'filter',
		focusToken = 0,
		placeholder = 'Search within this track list',
		oninput,
		onclear,
		onescapeclear,
		onfocuschange
	}: {
		value: string;
		mode?: 'filter' | 'find' | 'collection';
		/** Bump to focus + select the input. */
		focusToken?: number;
		placeholder?: string;
		oninput: (next: string) => void;
		onclear: () => void;
		onescapeclear?: () => void;
		onfocuschange?: (focused: boolean) => void;
	} = $props();

	let inputEl = $state<HTMLInputElement | null>(null);

	$effect(() => {
		if (focusToken <= 0 || inputEl === null) return;
		inputEl.focus();
		inputEl.select();
	});

	const modeLabel = $derived(
		mode === 'find' ? 'FIND' : mode === 'collection' ? 'ALL' : 'LIST'
	);
	const modeTitle = $derived(
		mode === 'find'
			? 'In-place find - highlights matches (3+ chars), does not filter'
			: mode === 'collection'
				? 'Whole collection FTS5 search'
				: 'Filter within this track list'
	);
</script>

<label class="rb-search" class:find={mode === 'find'} class:all={mode === 'collection'} title={modeTitle}>
	<span class="mode" aria-hidden="true">{modeLabel}</span>
	<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">
		<circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" stroke-width="1.5" />
		<path d="M10.5 10.5L14 14" stroke="currentColor" stroke-width="1.5" />
	</svg>
	<input
		bind:this={inputEl}
		type="text"
		{value}
		{placeholder}
		spellcheck="false"
		autocomplete="off"
		oninput={(e) => oninput(e.currentTarget.value)}
		onfocus={() => onfocuschange?.(true)}
		onblur={() => onfocuschange?.(false)}
		onkeydown={(e) => {
			if (e.key !== 'Escape') return;
			e.preventDefault();
			e.stopPropagation();
			(onescapeclear ?? onclear)();
		}}
	/>
	{#if value.trim() !== ''}
		<button
			type="button"
			class="clear"
			title="Clear search and return"
			aria-label="Clear search"
			onclick={(e) => {
				e.preventDefault();
				onclear();
			}}
		>
			×
		</button>
	{/if}
</label>

<style>
	.rb-search {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		width: 240px;
		height: 18px;
		padding: 0 4px 0 6px;
		background: #0a0c0f;
		border: 1px solid var(--rb-border);
		border-radius: 9px;
		color: var(--rb-text-dim);
	}
	.rb-search.find {
		border-color: color-mix(in srgb, var(--rb-yellow, #e8a13a) 55%, var(--rb-border));
	}
	.rb-search.all {
		border-color: color-mix(in srgb, var(--rb-accent, #2f6fd6) 55%, var(--rb-border));
	}
	.mode {
		flex: none;
		font-size: 8px;
		letter-spacing: 0.06em;
		font-weight: 600;
		color: var(--rb-text-dim);
		line-height: 1;
	}
	.rb-search.find .mode {
		color: var(--rb-yellow, #e8a13a);
	}
	.rb-search.all .mode {
		color: var(--rb-accent, #2f6fd6);
	}
	input {
		flex: 1;
		min-width: 0;
		background: transparent;
		border: none;
		outline: none;
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
	}
	input::placeholder {
		color: var(--rb-text-dim);
	}
	.clear {
		flex: none;
		width: 14px;
		height: 14px;
		padding: 0;
		border: none;
		border-radius: 50%;
		background: transparent;
		color: var(--rb-text-dim);
		font-size: 12px;
		line-height: 1;
		cursor: pointer;
	}
	.clear:hover {
		color: var(--rb-text);
		background: rgba(255, 255, 255, 0.08);
	}
</style>
