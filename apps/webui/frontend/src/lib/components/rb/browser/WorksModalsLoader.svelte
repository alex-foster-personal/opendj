<script lang="ts" module>
	import { firstUseImport } from './track-table-lazy-panels';

	// Module scope: one import per document even if the table remounts this.
	const lazy = firstUseImport(() => import('./WorksModals.svelte'));
</script>

<script lang="ts">
	/**
	 * Lazy mount point for the track table's works modals (#10272, PERF-BOOT-02).
	 *
	 * TrackTable mounts this through track-table-support.ts under the name
	 * WorksModals and keeps a bind:this handle to it. A static import put the
	 * works editor, link and merge modals in the boot bundle (PR #7901 raised
	 * the performance budget for them), so the real module is fetched by the
	 * first call that needs it, the pattern QuickDrawMenuLoader uses.
	 *
	 * Requirements (mini-PRD):
	 *   ✔︎ WorksModals.svelte is not imported until the first stackTracks call
	 *     or the first works modal, and nothing is rendered before then.
	 *     [if] this loader statically imports WorksModals.svelte [then] ⛔️
	 *     [if] the module is imported more than once [then] ⛔️
	 *   ✔︎ A call made before the module lands is replayed once it is up, with
	 *     the same arguments.
	 *     [if] the first stackTracks call is dropped [then] ⛔️
	 *   ✔︎ A failed fetch is shown inline with the error text and recovered by a
	 *     fresh document on the user's click; the import is tried once per
	 *     document (the module map keeps a failed chunk failed).
	 *     [if] the import rejects and nothing is shown [then] ⛔️
	 *     [if] the error promises a same-document retry [then] ⛔️
	 */
	import { tick, type Component } from 'svelte';
	import { worksView } from '$lib/rb/works-overlay.svelte';
	import { forwardWorksMethods, type WorksModalsMethods } from './track-table-lazy-panels';

	let Inner: Component<Record<string, never>, WorksModalsMethods> | null = $state(null);
	let inner: WorksModalsMethods | null = $state(null);
	let loadError: string | null = $state(null);
	let errorVisible = $state(false);

	async function ensureInner(): Promise<WorksModalsMethods> {
		try {
			const m = await lazy.load();
			if (Inner === null) {
				Inner = m.default as unknown as Component<Record<string, never>, WorksModalsMethods>;
			}
			await tick();
			if (inner === null) throw new Error('works modals did not mount');
			return inner;
		} catch (exc) {
			if (loadError === null) console.error('[works] works modals failed to load', exc);
			loadError = lazy.failure ?? (exc instanceof Error ? exc.message : String(exc));
			errorVisible = true;
			throw exc;
		}
	}

	const forward = forwardWorksMethods(ensureInner);

	export function stackTracks(ids: string[], title: string): Promise<void> {
		return forward.stackTracks(ids, title);
	}

	// Works modals opened straight through worksView (works menu, track table).
	$effect(() => {
		if (worksView.modal !== null && Inner === null && !lazy.started) {
			void ensureInner().catch(() => {});
		}
	});
</script>

{#if Inner}
	<Inner bind:this={inner} />
{:else if errorVisible && loadError !== null}
	<div class="works-load-error rb-panel" role="alert" data-testid="works-modals-load-error">
		<span>Works editor failed to load: {loadError}</span>
		<button type="button" class="works-load-close" onclick={() => location.reload()}>Reload</button>
		<button type="button" class="works-load-close" onclick={() => (errorVisible = false)}
			>Close</button
		>
	</div>
{/if}

<style>
	.works-load-error {
		position: fixed;
		top: 48px;
		right: 12px;
		z-index: 60;
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
	.works-load-close {
		background: transparent;
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
		font-size: 11px;
		padding: 2px 8px;
		cursor: pointer;
	}
</style>
