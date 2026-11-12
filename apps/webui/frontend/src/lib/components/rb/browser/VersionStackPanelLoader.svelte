<script lang="ts" module>
	import { firstUseImport } from './track-table-lazy-panels';

	// Module scope: one import per document even if the table remounts this.
	const lazy = firstUseImport(() => import('./VersionStackPanel.svelte'));
</script>

<script lang="ts">
	/**
	 * Lazy mount point for the version stack panel (#10272, PERF-BOOT-02).
	 *
	 * TrackTable mounts this through track-table-support.ts under the name
	 * VersionStackPanel. The real panel is fetched the first time a stack is
	 * opened (a non-null stableId), so it leaves the boot bundle (pays back
	 * PR #7901), the pattern QuickDrawMenuLoader uses.
	 *
	 * Requirements (mini-PRD):
	 *   ✔︎ With a null stableId this renders nothing and imports nothing.
	 *     [if] this loader statically imports VersionStackPanel.svelte [then] ⛔️
	 *     [if] a null stack id fetches the panel [then] ⛔️
	 *   ✔︎ A failed fetch is shown inline with the error text and recovered by a
	 *     fresh document on the user's click; the import is tried once per
	 *     document.
	 *     [if] the import rejects and nothing is shown [then] ⛔️
	 *     [if] the error promises a same-document retry [then] ⛔️
	 */
	import type { Component } from 'svelte';
	import { requestStackPanel } from './track-table-lazy-panels';

	type PanelProps = {
		stableId: string;
		playlistId: string | null;
		rowPosition: number | undefined;
		top: number;
		onclose: () => void;
	};

	let {
		stableId,
		playlistId,
		rowPosition,
		top,
		onclose
	}: Omit<PanelProps, 'stableId'> & { stableId: string | null } = $props();

	let Panel = $state<Component<PanelProps> | null>(null);
	let loadError = $state<string | null>(null);
	let errorVisible = $state(false);

	$effect(() => {
		if (stableId === null || Panel !== null) return;
		requestStackPanel(stableId, lazy);
		lazy.load().then(
			(m) => {
				Panel = m.default;
			},
			(exc: unknown) => {
				if (loadError === null) console.error('[works] version stack panel failed to load', exc);
				loadError = lazy.failure ?? (exc instanceof Error ? exc.message : String(exc));
				errorVisible = true;
			}
		);
	});
</script>

{#if stableId !== null && Panel}
	<Panel {stableId} {playlistId} {rowPosition} {top} {onclose} />
{:else if stableId !== null && errorVisible && loadError !== null}
	<div
		class="stack-load-error rb-panel"
		role="alert"
		data-testid="version-stack-load-error"
		style:top="{top}px"
	>
		<span>Version stack failed to load: {loadError}</span>
		<button type="button" class="stack-load-close" onclick={() => location.reload()}>Reload</button>
		<button
			type="button"
			class="stack-load-close"
			onclick={() => {
				errorVisible = false;
				onclose();
			}}>Close</button
		>
	</div>
{/if}

<style>
	.stack-load-error {
		position: absolute;
		left: 12px;
		z-index: 30;
		display: flex;
		gap: 10px;
		align-items: center;
		max-width: 420px;
		padding: 8px 12px;
		border: 1px solid var(--rb-red);
		color: var(--rb-text);
		font-size: 12px;
		overflow-wrap: anywhere;
	}
	.stack-load-close {
		background: transparent;
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
		font-size: 11px;
		padding: 2px 8px;
		cursor: pointer;
	}
</style>
