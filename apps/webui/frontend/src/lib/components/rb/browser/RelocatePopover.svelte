<script lang="ts">
	// FLOW-07: the track-menu "Relocate" item was permanently inert (no `run`
	// at all) - this reuses the exact API the /reconcile page already calls
	// (getRelocateCandidates / applyRelocate), so relocate becomes reachable
	// from the row a user actually right-clicks instead of only from a
	// disconnected page they'd have to already know the URL of.
	import { onMount } from 'svelte';
	import { getTrack } from '$lib/api';
	import {
		applyRelocate,
		getRelocateCandidates,
		RelocateApplyError,
		type RelocateCandidate
	} from '$lib/reconcile-api';
	import { rekordboxWritebackRefusal } from '$lib/rb/rekordbox-writeback.svelte';
	import { pushToast } from '$lib/stores.svelte';
	import RelocatePopoverView from './RelocatePopoverView.svelte';

	let { stableId, trackTitle, x, y, onclose, onrelocated }: {
		stableId: string;
		trackTitle: string | null;
		x: number;
		y: number;
		onclose: () => void;
		onrelocated: () => void;
	} = $props();

	let loading = $state(true);
	let applying = $state(false);
	let candidates = $state<RelocateCandidate[]>([]);
	let originalPath = $state<string | null>(null);
	let vendorId = $state<string | null>(null);
	let errorMessage = $state<string | null>(null);

	// Same gate the reconcile page uses: relocate patches the live rekordbox
	// database, so it is inert whenever the daemon is in one-way import mode.
	const writebackRefusal = $derived(rekordboxWritebackRefusal());

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
</script>

<RelocatePopoverView
	{x}
	{y}
	{writebackRefusal}
	{loading}
	{errorMessage}
	{candidates}
	{applying}
	onpick={(candidate) => void pickCandidate(candidate)}
	{onclose}
/>
