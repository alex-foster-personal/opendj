<script lang="ts">
	/**
	 * /tryer?out=<stable_id>&in=<stable_id>[&n=6&context_bars=4] (TRYER-14).
	 * Behind the `dj.tryer` build flag, default OFF: with the flag off or unread
	 * the page says so and requests nothing. Agents use the HTTP/CLI/MCP twins.
	 */
	import { page } from '$app/stores';
	import TryerPanel from '$lib/tryer/TryerPanel.svelte';
	import { tryerEnabled } from '$lib/tryer/tryer-flag';

	const enabled = $derived(tryerEnabled());
	const outId = $derived($page.url.searchParams.get('out'));
	const inId = $derived($page.url.searchParams.get('in'));
	const n = $derived(Number($page.url.searchParams.get('n') ?? '6'));
	const contextBars = $derived(Number($page.url.searchParams.get('context_bars') ?? '4'));
</script>

<svelte:head><title>Transition try-er</title></svelte:head>

{#if !enabled}
	<p>The transition try-er is off (build flag <code>dj.tryer</code>).</p>
{:else if outId === null || inId === null}
	<p role="alert">
		Name both tracks: <code>/tryer?out=&lt;stable_id&gt;&amp;in=&lt;stable_id&gt;</code>.
	</p>
{:else if !Number.isInteger(n) || !Number.isInteger(contextBars)}
	<p role="alert">n and context_bars must be whole numbers.</p>
{:else}
	<!-- Keyed: a new pair or size is a new panel, so no job, selection or audio carries over. -->
	{#key `${outId}\n${inId}\n${n}\n${contextBars}`}
		<TryerPanel {outId} {inId} {n} {contextBars} />
	{/key}
{/if}
