<script lang="ts">
	// add-remove-reorder-tracks: search-and-append control for the pane
	// header, shown only when a real (non-"all", non-blank) playlist is the
	// active pane. Debounced substring search against the real /tracks
	// endpoint (q= title/artist match) - no client-side track cache, no
	// fixture data; an empty library means an empty dropdown, not inert UI
	// (the control itself is always real, see PARITY-TODO house rule).
	import { listTracks, type Track } from '$lib/api';

	let { onadd }: { onadd: (stable_id: string) => void } = $props();

	let query = $state('');
	let results = $state<Track[]>([]);
	let open = $state(false);
	let loading = $state(false);
	let error = $state<string | null>(null);
	let debounceHandle: ReturnType<typeof setTimeout> | undefined;

	function onInput(next: string): void {
		query = next;
		if (debounceHandle !== undefined) clearTimeout(debounceHandle);
		const trimmed = next.trim();
		if (trimmed.length < 2) {
			results = [];
			open = false;
			return;
		}
		debounceHandle = setTimeout(() => void _search(trimmed), 200);
	}

	async function _search(q: string): Promise<void> {
		loading = true;
		error = null;
		try {
			const page = await listTracks({ q, limit: 8 });
			results = page.items;
			open = true;
		} catch (exc) {
			error = String(exc);
			results = [];
		} finally {
			loading = false;
		}
	}

	function pick(track: Track): void {
		onadd(track.stable_id);
		query = '';
		results = [];
		open = false;
	}

	function onBlur(): void {
		// Defer so a click on a dropdown row still registers before it closes.
		setTimeout(() => (open = false), 150);
	}
</script>

<div class="add-track">
	<label class="add-track-search">
		<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">
			<path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="1.5" />
		</svg>
		<input
			type="text"
			value={query}
			placeholder="Add track (title/artist)"
			spellcheck="false"
			autocomplete="off"
			oninput={(e) => onInput(e.currentTarget.value)}
			onfocus={() => (open = results.length > 0)}
			onblur={onBlur}
		/>
	</label>
	{#if open}
		<ul class="add-track-results">
			{#if loading}
				<li class="status">searching...</li>
			{:else if error !== null}
				<li class="status error">{error}</li>
			{:else if results.length === 0}
				<li class="status">no matches</li>
			{:else}
				{#each results as track (track.stable_id)}
					<li>
						<button type="button" onmousedown={(e) => e.preventDefault()} onclick={() => pick(track)}>
							<span class="title">{track.title ?? track.stable_id}</span>
							<span class="artist">{track.artist ?? ''}</span>
						</button>
					</li>
				{/each}
			{/if}
		</ul>
	{/if}
</div>

<style>
	.add-track {
		position: relative;
		flex: none;
	}
	.add-track-search {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		width: 190px;
		height: 18px;
		padding: 0 6px;
		background: #0a0c0f;
		border: 1px solid var(--rb-border);
		border-radius: 9px;
		color: var(--rb-text-dim);
	}
	.add-track-search input {
		flex: 1;
		min-width: 0;
		background: transparent;
		border: none;
		outline: none;
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
	}
	.add-track-search input::placeholder {
		color: var(--rb-text-dim);
	}
	.add-track-results {
		position: absolute;
		top: 20px;
		right: 0;
		z-index: 5;
		width: 260px;
		max-height: 220px;
		overflow-y: auto;
		margin: 0;
		padding: 2px 0;
		list-style: none;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 4px;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
	}
	.add-track-results li.status {
		padding: 4px 8px;
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
	}
	.add-track-results li.status.error {
		color: var(--rb-red);
	}
	.add-track-results button {
		display: flex;
		align-items: baseline;
		gap: 6px;
		width: 100%;
		padding: 3px 8px;
		background: transparent;
		border: none;
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		text-align: left;
		cursor: pointer;
	}
	.add-track-results button:hover {
		background: var(--rb-accent);
		color: #fff;
	}
	.add-track-results .title {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.add-track-results .artist {
		flex: none;
		color: var(--rb-text-dim);
	}
	.add-track-results button:hover .artist {
		color: #fff;
	}
</style>
