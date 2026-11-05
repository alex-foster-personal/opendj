<script lang="ts" generics="P extends object">
	// Mounts one React twin inside a Svelte tree (REACT-01, ADR "React twins").
	//
	// `load` is a dynamic import of the twin, and react / react-dom are
	// dynamically imported here too, so a build with every React flag off never
	// downloads React at all. The host is `display: contents`, so the twin's
	// root element sits in the parent's layout exactly where the Svelte
	// original would. Props re-render the root whenever the `props` expression
	// changes; pass plain values or snapshots, never a live per-frame source
	// (React re-renders only when this effect re-runs).
	import { onMount } from 'svelte';
	import type { FunctionComponent } from 'react';
	import type { Root } from 'react-dom/client';

	let {
		load,
		props
	}: {
		load: () => Promise<{ default: FunctionComponent<P> }>;
		props: P;
	} = $props();

	let host: HTMLSpanElement;
	let render: ((next: P) => void) | null = $state.raw(null);
	// A twin that cannot load must not leave a silently empty control: the
	// island shows the failure in place instead (brittle fail-fast).
	let failure: string | null = $state(null);

	// Mount once: the twin and its loader are fixed for the island's life.
	onMount(() => {
		let cancelled = false;
		let root: Root | null = null;
		void Promise.all([import('react'), import('react-dom/client'), load()]).then(
			([react, dom, twin]) => {
				if (cancelled) return;
				const mounted = dom.createRoot(host);
				root = mounted;
				render = (next) => mounted.render(react.createElement(twin.default, next));
			},
			(exc: unknown) => {
				console.error('[react-island] twin failed to load', exc);
				if (!cancelled) failure = exc instanceof Error ? exc.message : String(exc);
			}
		);
		return () => {
			cancelled = true;
			render = null;
			root?.unmount();
			root = null;
		};
	});

	$effect(() => {
		const next = props;
		render?.(next);
	});
</script>

<span class="react-island" data-react-island bind:this={host}></span>
{#if failure !== null}
	<span
		class="react-island-failed"
		role="alert"
		data-react-island-failed
		title={`React twin failed to load: ${failure}`}>React control failed to load</span
	>
{/if}

<style>
	.react-island {
		display: contents;
	}
	.react-island-failed {
		color: var(--rb-red);
		font-size: 11px;
	}
</style>
