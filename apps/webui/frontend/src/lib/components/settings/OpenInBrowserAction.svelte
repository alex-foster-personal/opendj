<script lang="ts">
	/**
	 * Settings > "Open in browser" (UIURL-03). States and copy live in
	 * $lib/settings/open-in-browser; this component only fetches, renders and
	 * acts. Agent twins: `opendj open browser`, GET /api/v1/ui-url.
	 */
	import { onMount } from 'svelte';
	import { getUiUrl } from '$lib/api';
	import { nativeShellKind, openExternal } from '$lib/shell/native-shell';
	import {
		OPEN_IN_BROWSER_LABEL,
		openInBrowserView,
		type UiAddress
	} from '$lib/settings/open-in-browser';

	let { compact = false }: { compact?: boolean } = $props();

	let address = $state<UiAddress | null>(null);
	let error = $state<string | null>(null);
	let actionError = $state<string | null>(null);
	let copied = $state(false);

	const view = $derived(
		openInBrowserView({
			address,
			error,
			shell: nativeShellKind(),
			pageHostname: globalThis.location?.hostname ?? ''
		})
	);

	onMount(async () => {
		try {
			address = await getUiUrl();
		} catch (exc) {
			error = exc instanceof Error ? exc.message : String(exc);
		}
	});

	async function openInBrowser(url: string): Promise<void> {
		actionError = null;
		try {
			await openExternal(url);
		} catch (exc) {
			actionError = `Could not open the browser: ${exc instanceof Error ? exc.message : String(exc)}`;
		}
	}

	async function copyAddress(url: string): Promise<void> {
		actionError = null;
		try {
			await navigator.clipboard.writeText(url);
			copied = true;
		} catch (exc) {
			actionError = `Could not copy: ${exc instanceof Error ? exc.message : String(exc)}`;
		}
	}
</script>

{#if view.kind !== 'hidden'}
	<span class="oib" class:compact data-testid="open-in-browser" data-state={view.kind}>
		{#if view.kind === 'open'}
			<button
				type="button"
				class="oib-btn"
				onclick={() => void openInBrowser(view.url)}
				title={view.title}
			>
				{OPEN_IN_BROWSER_LABEL}
			</button>
			<code class="oib-url" title={view.title}>{view.url}</code>
		{:else if view.kind === 'copy'}
			<code class="oib-url" title={view.title}>{view.url}</code>
			<button
				type="button"
				class="oib-btn"
				onclick={() => void copyAddress(view.url)}
				title={view.title}
			>
				{copied ? 'Copied' : 'Copy address'}
			</button>
		{:else}
			<button type="button" class="oib-btn" disabled title={view.title}>
				{OPEN_IN_BROWSER_LABEL}
			</button>
		{/if}
		{#if actionError !== null}
			<span class="oib-err" title={actionError}>{actionError}</span>
		{/if}
	</span>
{/if}

<style>
	.oib {
		display: inline-flex;
		align-items: center;
		gap: 0.5rem;
		flex-wrap: wrap;
	}
	.oib-btn {
		padding: 6px 12px;
		border-radius: 8px;
		border: 1px solid var(--border, #1c222c);
		background: var(--surface-raised, #1a212c);
		color: var(--fg, #e6e9ef);
		cursor: pointer;
		font-size: 0.85rem;
	}
	.oib-btn:disabled {
		opacity: 0.45;
		cursor: default;
	}
	.oib-url {
		font-size: 0.85rem;
		color: var(--muted, #9aa4b2);
		user-select: all;
	}
	.oib.compact .oib-url {
		font-size: 0.75rem;
	}
	.oib-err {
		color: var(--danger, #ff5a5a);
		font-size: 0.8rem;
	}
</style>
