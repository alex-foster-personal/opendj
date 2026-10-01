<script lang="ts">
	/**
	 * Low-disk indicator for the health area (STEM-43). Self-contained on
	 * purpose: it owns its fetch, its poll and its styles, so mounting it is
	 * one tag and it adds no state to the panel that hosts it.
	 *
	 * Decides nothing. `stemCacheHealthDot` in `$lib/rb/stem-cache-health`
	 * owns the state and the wording.
	 */
	import { onMount } from 'svelte';
	import { api, unwrap } from '$lib/api/client';
	import {
		stemCacheHealthDot,
		type StemCacheStatusView
	} from '$lib/rb/stem-cache-health';

	const POLL_MS = 60_000;

	let status = $state<StemCacheStatusView | null>(null);
	let loadError = $state<string | null>(null);
	const dot = $derived(stemCacheHealthDot(status, loadError));

	async function load(): Promise<void> {
		try {
			status = await unwrap(api.GET('/api/v1/stems/cache/status', {}));
			loadError = null;
		} catch (error: unknown) {
			loadError = error instanceof Error ? error.message : String(error);
		}
	}

	onMount(() => {
		void load();
		const timer = setInterval(() => {
			if (!document.hidden) void load();
		}, POLL_MS);
		return () => clearInterval(timer);
	});
</script>

<button
	type="button"
	class="stem-cache-dot"
	class:complete={dot.state === 'complete'}
	class:incomplete={dot.state === 'incomplete'}
	class:unavailable={dot.state === 'unavailable'}
	class:error={dot.state === 'error'}
	data-testid="stem-cache-health-dot"
	data-state={dot.state}
	aria-label={`${dot.label}: ${dot.detail}`}
	title={`${dot.label}: ${dot.detail}`}
>
	<span aria-hidden="true"></span>
</button>

<style>
	.stem-cache-dot {
		position: relative;
		width: 12px;
		height: 12px;
		padding: 0;
		border: none;
		background: transparent;
		cursor: help;
	}
	.stem-cache-dot > span {
		display: block;
		width: 7px;
		height: 7px;
		border-radius: 50%;
		background: #3a4048;
		box-shadow: inset 0 0 0 1px #23282f;
	}
	.stem-cache-dot.complete > span {
		background: var(--rb-green, #35c04f);
		box-shadow: 0 0 4px color-mix(in srgb, var(--rb-green, #35c04f) 70%, transparent);
	}
	.stem-cache-dot.incomplete > span {
		background: var(--rb-orange, #e8912d);
	}
	.stem-cache-dot.unavailable > span {
		background: var(--rb-text-dim);
	}
	.stem-cache-dot.error > span {
		background: var(--rb-red, #d9534f);
	}
</style>
