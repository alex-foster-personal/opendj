<script lang="ts">
	// CMDK-05: the library's filters as a row of icons under the Cmd K input,
	// lined up with the input's text. Hover or focus shows the library's own
	// name and hover sentence at once (no slow native title), plus the Alt key.
	import {
		CMDK_FILTER_KEYS,
		LIBRARY_FILTER_LABELS,
		LIBRARY_FILTER_ORDER,
		type LibraryFilterId
	} from '$lib/rb/library-filter-labels';

	let {
		states,
		onToggle
	}: {
		states: Record<LibraryFilterId, boolean>;
		onToggle: (id: LibraryFilterId) => void;
	} = $props();

	const onCount = $derived(LIBRARY_FILTER_ORDER.filter((id) => states[id]).length);
</script>

<div class="cmdk-filters" role="group" aria-label="Library filters" data-testid="command-bar-filters">
	{#each LIBRARY_FILTER_ORDER as id (id)}
		{@const label = LIBRARY_FILTER_LABELS[id]}
		<button
			type="button"
			class="fb"
			class:on={states[id]}
			aria-pressed={states[id]}
			aria-label={label.name}
			aria-describedby={`cmdk-filter-tip-${id}`}
			data-testid={`command-bar-filter-${id}`}
			onmousedown={(event) => event.preventDefault()}
			onclick={() => onToggle(id)}
		>
			<!-- Plain markup, not a raw-HTML string: the app had no other raw-HTML block, so one
				here pulled Svelte's html runtime into the "/" first-paint chunk (bundle budget). -->
			<svg viewBox="0 0 14 14" aria-hidden="true">
				{#if id === 'broken'}
					<path
						d="M6 4.5 7.5 3a2.5 2.5 0 0 1 3.5 3.5L9.5 8M8 9.5 6.5 11A2.5 2.5 0 0 1 3 7.5L4.5 6M2 2l1.2 1.2M12 12l-1.2-1.2"
					/>
				{:else if id === 'remixes'}
					<path d="M2.5 6a4.5 4.5 0 0 1 8-2.5M11.5 8a4.5 4.5 0 0 1-8 2.5" />
					<path d="M10.8 1.2v2.6H8.2M3.2 12.8v-2.6h2.6" />
				{:else if id === 'vocals'}
					<rect x="5" y="1.5" width="4" height="7" rx="2" />
					<path d="M3 6.5a4 4 0 0 0 8 0M7 10.5V13M5 13h4" />
				{:else if id === 'compatible'}
					<circle cx="5" cy="7" r="3.5" />
					<circle cx="9" cy="7" r="3.5" />
				{:else}
					<path d="M7 1.5v7M4 5.8 7 8.8l3-3M2 10.5v2h10v-2" />
				{/if}
			</svg>
			<span class="tip" id={`cmdk-filter-tip-${id}`}
				><b>{label.name}<kbd>Alt {CMDK_FILTER_KEYS[id]}</kbd></b>{label.tip}</span
			>
		</button>
	{/each}
	<span class="count" title="How many library filters are on">{onCount > 0 ? `${onCount} on` : ''}</span>
</div>

<style>
	.cmdk-filters {
		display: flex;
		gap: 2px;
		margin-left: -4px;
	}
	.fb {
		position: relative;
		width: 24px;
		height: 24px;
		padding: 0;
		border: 0;
		border-radius: 4px;
		background: transparent;
		color: var(--rb-text-dim, #8a94a3);
		opacity: 0.65;
		cursor: pointer;
		display: grid;
		place-items: center;
	}
	.fb svg {
		width: 14px;
		height: 14px;
		fill: none;
		stroke: currentColor;
		stroke-width: 1.3;
		stroke-linecap: round;
		stroke-linejoin: round;
	}
	.fb:hover {
		opacity: 1;
		background: var(--rb-panel, #151920);
	}
	.fb.on {
		opacity: 1;
		color: var(--rb-cmdk-pick, #a46ef0);
		background: color-mix(in srgb, var(--rb-cmdk-pick, #a46ef0) 16%, transparent);
	}
	.fb:focus-visible {
		outline: 2px solid var(--rb-cmdk-pick, #a46ef0);
		outline-offset: 1px;
	}
	.tip {
		position: absolute;
		top: calc(100% + 6px);
		left: 0;
		z-index: 5;
		width: max-content;
		max-width: 260px;
		padding: 6px 8px;
		border-radius: 5px;
		background: var(--rb-bg, #0f1216);
		border: 1px solid var(--rb-border, #2c323c);
		color: var(--rb-text-dim, #8a94a3);
		font: 11px/1.35 var(--rb-font, system-ui, sans-serif);
		text-align: left;
		white-space: normal;
		pointer-events: none;
		opacity: 0;
		transform: translateY(-2px);
		transition:
			opacity 90ms,
			transform 90ms;
		box-shadow: 0 8px 20px rgb(0 0 0 / 35%);
	}
	.tip b {
		display: block;
		color: var(--rb-text, #e6e9ee);
		font-weight: 600;
		font-size: 12px;
	}
	.tip kbd {
		background: transparent;
		font: 10px var(--rb-font, system-ui, sans-serif);
		color: var(--rb-text-dim, #8a94a3);
		border: 1px solid var(--rb-border, #2c323c);
		border-radius: 3px;
		padding: 0 3px;
		margin-left: 6px;
	}
	.fb:hover .tip,
	.fb:focus-visible .tip {
		opacity: 1;
		transform: none;
	}
	.count {
		align-self: center;
		margin-left: 6px;
		font-size: 10px;
		color: var(--rb-text-dim, #8a94a3);
	}
	@media (prefers-reduced-motion: reduce) {
		.tip {
			transition: none;
		}
	}
</style>
