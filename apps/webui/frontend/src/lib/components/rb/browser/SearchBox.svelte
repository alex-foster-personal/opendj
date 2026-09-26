<script lang="ts">
	import { untrack } from 'svelte';
	import ControlExplainer from '../deck/ControlExplainer.svelte';
	import { SEARCH_QUERY_HELP } from '$lib/rb/browser-search-query';

	// Browser pane search (SCREENSHOT-SPEC 5c). Modes:
	//   filter     - Cmd+F: client filter within this track list
	//   find       - Cmd+F again: in-place highlight (no filter), ≥3 chars
	//   collection - Cmd+Shift+F: whole-collection FTS5
	//
	// Wrapped in ControlExplainer (pin 7ca47b21ead7, issue #936) so the
	// `field:operator:value` search grammar (browser-search-query.ts) is
	// discoverable - the same hover/focus pattern already used for stem tags
	// and deck controls, reused rather than a third explainer component.
	// ControlExplainer opens on pointer hover AND on focus-in, which is what
	// puts it up in the EMPTY state the moment the box is focused, before a
	// single character is typed - "a syntax nobody can discover is a syntax
	// nobody uses".
	const SEARCH_EXPLAINER_BULLETS = SEARCH_QUERY_HELP.map((h) => `${h.example} - ${h.hint}`);
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

	// Local echo of what has been typed (PERF-R5 Q9).
	//
	// The owner now debounces the row recompute, so `value` lags the caret by
	// up to LOCAL_FILTER_DEBOUNCE_MS. Rendering `value` directly would make the
	// input itself feel laggy, and would leave the clear button and an Escape
	// pressed mid-burst acting on a stale string. The draft is therefore the
	// rendered truth for keystrokes, and snaps back whenever the OWNER moves
	// `value` (genre chip, clear, pane switch, nav restore) - the only two
	// writers there are.
	// untrack: both seed from the INITIAL prop on purpose - the $effect below
	// is what tracks it afterwards.
	let draft = $state(untrack(() => value));
	let ownerValue = untrack(() => value);
	$effect(() => {
		if (value === ownerValue) return;
		ownerValue = value;
		draft = value;
	});

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

<!-- Native `title` dropped in favor of aria-label: ControlExplainer's own
     popover heading carries this text, and the doc comment on that
     component names exactly this overlap as the thing to avoid. -->
<ControlExplainer title="Search syntax" bullets={[modeTitle, ...SEARCH_EXPLAINER_BULLETS]}>
	<label class="rb-search" class:find={mode === 'find'} class:all={mode === 'collection'} aria-label={modeTitle}>
		<span class="mode" aria-hidden="true">{modeLabel}</span>
		<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">
			<circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" stroke-width="1.5" />
			<path d="M10.5 10.5L14 14" stroke="currentColor" stroke-width="1.5" />
		</svg>
		<input
			bind:this={inputEl}
			type="text"
			value={draft}
			{placeholder}
			spellcheck="false"
			autocomplete="off"
			oninput={(e) => {
				// The caret never waits: echo first, then hand the owner the
				// keystroke it will debounce.
				draft = e.currentTarget.value;
				oninput(draft);
			}}
			onfocus={() => onfocuschange?.(true)}
			onblur={() => onfocuschange?.(false)}
			onkeydown={(e) => {
				if (e.key !== 'Escape') return;
				e.preventDefault();
				e.stopPropagation();
				// Escape pressed mid-burst: the owner's `value` may still be the
				// pre-burst string, so clearing it would not move the prop and the
				// echo would keep showing what was typed. Clear the echo here.
				draft = '';
				(onescapeclear ?? onclear)();
			}}
		/>
		{#if draft.trim() !== ''}
			<button
				type="button"
				class="clear"
				title="Clear search and return"
				aria-label="Clear search"
				onclick={(e) => {
					e.preventDefault();
					draft = '';
					onclear();
				}}
			>
				×
			</button>
		{/if}
	</label>
</ControlExplainer>

<style>
	.rb-search {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		width: min(240px, 100%);
		max-width: 240px;
		min-width: 0;
		flex: 1 1 120px;
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
