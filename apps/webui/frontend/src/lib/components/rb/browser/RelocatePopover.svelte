<script lang="ts">
	// FLOW-07: the track-menu "Relocate" item was permanently inert (no `run`
	// at all) - this reuses the exact API the /reconcile page already calls
	// (getRelocateCandidates / applyRelocate), so relocate becomes reachable
	// from the row a user actually right-clicks instead of only from a
	// disconnected page they'd have to already know the URL of.
	import { onMount, tick } from 'svelte';
	import { pointFloatingAction } from '$lib/ui/clamp-to-viewport';
	import { getTrack } from '$lib/api';
	import {
		applyRelocate,
		getRelocateCandidates,
		RelocateApplyError,
		type RelocateCandidate
	} from '$lib/reconcile-api';
	import { rekordboxWritebackRefusal } from '$lib/rb/rekordbox-writeback.svelte';
	import { pushToast } from '$lib/stores.svelte';

	let { stableId, trackTitle, x, y, onclose, onrelocated }: {
		stableId: string;
		trackTitle: string | null;
		x: number;
		y: number;
		onclose: () => void;
		onrelocated: () => void;
	} = $props();

	let menu = $state<HTMLDivElement | null>(null);
	let loading = $state(true);
	let applying = $state(false);
	let candidates = $state<RelocateCandidate[]>([]);
	let originalPath = $state<string | null>(null);
	let vendorId = $state<string | null>(null);
	let errorMessage = $state<string | null>(null);

	// Same gate the reconcile page uses: relocate patches the live rekordbox
	// database, so it is inert whenever the daemon is in one-way import mode.
	const writebackRefusal = $derived(rekordboxWritebackRefusal());

	// Placement is pointFloatingAction's job, not a one-shot clamp here: the
	// menu opens on a single loading row and grows once its items arrive, so
	// a clamp measured at open left the grown menu hanging off the viewport
	// bottom (unclickable - a fixed node cannot be scrolled into view). The
	// action re-clamps from (x, y) on every size change.
	async function focusMenu(): Promise<void> {
		await tick();
		menu?.focus();
	}

	$effect(() => {
		void x;
		void y;
		void focusMenu();
	});

	onMount(() => {
		void (async () => {
			try {
				const found = await getRelocateCandidates(stableId);
				candidates = found.candidates;
				originalPath = found.original_path;
				vendorId = found.vendor_id;
			} catch (error) {
				errorMessage = error instanceof Error ? error.message : 'Failed to find candidates';
			} finally {
				loading = false;
			}
		})();
	});

	async function pickCandidate(candidate: RelocateCandidate): Promise<void> {
		if (writebackRefusal !== null || originalPath === null) return;
		applying = true;
		try {
			const { etag } = await getTrack(stableId);
			await applyRelocate(stableId, candidate.path, {
				ifMatch: etag,
				expectedOriginalPath: originalPath,
				expectedVendorId: vendorId,
				expectedCandidateIdentity: candidate.identity_token
			});
			pushToast(`Relocated "${trackTitle ?? stableId}"`, 'info');
			onrelocated();
			onclose();
		} catch (exc) {
			if (exc instanceof RelocateApplyError) {
				pushToast(`Relocate failed (${exc.code}): ${exc.message}`, 'error');
			} else {
				pushToast(`Relocate failed: ${exc}`, 'error');
			}
		} finally {
			applying = false;
		}
	}

	function onKeydown(event: KeyboardEvent): void {
		if (event.key === 'Escape') {
			event.preventDefault();
			onclose();
		}
	}

	function onOutsidePointerDown(event: PointerEvent): void {
		if (menu !== null && event.target instanceof Node && !menu.contains(event.target)) onclose();
	}
</script>

<svelte:window onkeydown={onKeydown} onpointerdowncapture={onOutsidePointerDown} />

<div
	bind:this={menu}
	class="track-relocate-menu"
	data-testid="track-relocate-menu"
	role="menu"
	tabindex="-1"
	use:pointFloatingAction={{ x, y }}
>
	{#if writebackRefusal !== null}
		<button type="button" role="menuitem" disabled title={writebackRefusal}>{writebackRefusal}</button>
	{:else if loading}
		<button type="button" role="menuitem" disabled>Finding candidates...</button>
	{:else if errorMessage !== null}
		<button type="button" role="menuitem" disabled>{errorMessage}</button>
	{:else if candidates.length === 0}
		<button type="button" role="menuitem" disabled>No relocate candidates found</button>
	{:else}
		{#each candidates as candidate (candidate.path)}
			<button
				type="button"
				role="menuitem"
				disabled={applying}
				title={candidate.path}
				onclick={() => void pickCandidate(candidate)}
			>
				{candidate.path}
			</button>
		{/each}
	{/if}
</div>

<style>
	.track-relocate-menu {
		position: fixed;
		z-index: 1001;
		min-width: 220px;
		max-width: min(520px, calc(100vw - 16px));
		max-height: calc(100vh - 16px);
		overflow: auto;
		padding: 4px;
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		background: var(--rb-panel-raised);
		box-shadow: 0 5px 18px rgb(0 0 0 / 45%);
	}
	.track-relocate-menu button {
		display: block;
		width: 100%;
		padding: 5px 8px;
		border: 0;
		border-radius: 2px;
		background: transparent;
		color: var(--rb-text);
		font: inherit;
		text-align: left;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.track-relocate-menu button:not(:disabled):hover,
	.track-relocate-menu button:not(:disabled):focus-visible {
		background: var(--rb-select);
	}
	.track-relocate-menu button:disabled {
		color: var(--rb-text-dim);
		cursor: not-allowed;
	}
</style>
