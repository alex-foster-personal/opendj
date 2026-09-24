/**
 * The one CloudSync toast shared by two surfaces: the full `/cloudsync`
 * Status tab (`CloudSyncStatusTab.svelte`) and the in-place quick actions
 * popover (`CloudSyncQuickActions.svelte`, issue #3531). Both call Force
 * sync, so both need the identical bypass warning; a single copy here keeps
 * the wording one string instead of two, and keeps `$lib/stores.svelte`
 * imported from one cloudsync-owned module instead of both components.
 */
import { pushToast } from '$lib/stores.svelte';

export function warnForceSyncBypassesGate(): void {
	pushToast('Force sync bypasses Gig and playing-deck protection for one round.', 'warn');
}
