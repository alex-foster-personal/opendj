<script lang="ts">
	// Browser pane search (SCREENSHOT-SPEC 5c). Pure client-side filter input:
	// no server round-trip (RECON-BACKEND 2 warns against per-keystroke polling).
	let {
		value,
		placeholder = 'Search within this track list',
		oninput
	}: {
		value: string;
		placeholder?: string;
		oninput: (next: string) => void;
	} = $props();
</script>

<label class="rb-search">
	<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">
		<circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" stroke-width="1.5" />
		<path d="M10.5 10.5L14 14" stroke="currentColor" stroke-width="1.5" />
	</svg>
	<input
		type="text"
		{value}
		{placeholder}
		spellcheck="false"
		autocomplete="off"
		oninput={(e) => oninput(e.currentTarget.value)}
	/>
</label>

<style>
	.rb-search {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		width: 200px;
		height: 18px;
		padding: 0 6px;
		background: #0a0c0f;
		border: 1px solid var(--rb-border);
		border-radius: 9px;
		color: var(--rb-text-dim);
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
</style>
