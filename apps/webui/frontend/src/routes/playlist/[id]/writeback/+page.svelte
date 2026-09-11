<script lang="ts">
	import { page } from '$app/stores';
	import { onMount } from 'svelte';
	import {
		applyWriteback,
		getWritebackCapabilities,
		getWritebackPlan,
		getWritebackTargets,
		rollbackWriteback,
		type VendorCapability,
		type WritebackApplyResult,
		type WritebackPlan,
		type WritebackTarget,
		type WritebackVendor
	} from '$lib/rb/api-writeback';
	import { RbApiError } from '$lib/rb/api-rb-error';
	import {
		rekordboxWriteback,
		rekordboxWritebackRefusal
	} from '$lib/rb/rekordbox-writeback.svelte';
	import {
		canRollbackWriteback,
		planMatchesSelection,
		WritebackRequestGate,
		writebackPlanMutation,
		type WritebackSelection
	} from '$lib/rb/writeback-selection';

	let playlistId = $state('');
	let capabilities = $state<VendorCapability[] | null>(null);
	let selectedVendor = $state<WritebackVendor | null>(null);
	let targets = $state<WritebackTarget[]>([]);
	let targetId = $state('');
	let plan = $state<WritebackPlan | null>(null);
	let applyResult = $state<WritebackApplyResult | null>(null);
	let confirmed = $state(false);
	let rollbackConfirmed = $state(false);
	let error = $state<string | null>(null);
	let loading = $state(false);
	const requestGate = new WritebackRequestGate();

	// One-way import mode: APPLY writes the LIVE rekordbox master.db, so it is
	// inert while the daemon's gate is off. djay is a different vendor and a
	// different decision, so it is NOT gated here -- the refusal is scoped to
	// the selected vendor, exactly like the server's.
	//
	// ROLLBACK IS DELIBERATELY LEFT ENABLED. It only appears after an apply
	// already landed, and taking away the undo for a bad write is worse for
	// the user's data than the write itself. The server agrees: that surface
	// is mapped gated=False.
	const gateRefusal = $derived(rekordboxWritebackRefusal());
	const writebackRefusal = $derived(selectedVendor === 'rekordbox' ? gateRefusal : null);

	onMount(() => {
		playlistId = $page.params.id ?? '';
		if (!playlistId) throw new Error('playlist route param "id" missing');
		void rekordboxWriteback.probe();
		void refreshCapabilities();
		return () => requestGate.invalidate();
	});

	function selectedCapability(): VendorCapability | undefined {
		return capabilities?.find((capability) => capability.vendor === selectedVendor);
	}

	function selection(): WritebackSelection | null {
		const capability = selectedCapability();
		if (!selectedVendor || !capability?.target_path) return null;
		return {
			vendor: selectedVendor,
			target_mode: capability.target_mode,
			target_path: capability.target_path,
			target_id: targetId
		};
	}

	function clearPlan(clearResult = true): void {
		plan = null;
		if (clearResult) applyResult = null;
		confirmed = false;
		rollbackConfirmed = false;
	}

	function message(cause: unknown): string {
		return cause instanceof RbApiError ? cause.message : String(cause);
	}

	async function refreshCapabilities(): Promise<void> {
		const ticket = requestGate.capture({ vendor: '', target_mode: '', target_path: '', target_id: '' });
		loading = true;
		try {
			const next = await getWritebackCapabilities(playlistId);
			if (!requestGate.isCurrent(ticket, { vendor: '', target_mode: '', target_path: '', target_id: '' })) return;
			capabilities = next.vendors;
			selectedVendor = next.vendors.find((capability) => capability.available)?.vendor ?? null;
			targetId = '';
			clearPlan();
			await refreshTargets();
		} catch (cause) {
			if (requestGate.isCurrent(ticket, { vendor: '', target_mode: '', target_path: '', target_id: '' })) error = message(cause);
		} finally {
			if (requestGate.isCurrent(ticket, { vendor: '', target_mode: '', target_path: '', target_id: '' })) loading = false;
		}
	}

	async function refreshTargets(): Promise<void> {
		clearPlan();
		targetId = '';
		const selected = selection();
		if (selected === null) return;
		const ticket = requestGate.capture(selected);
		loading = true;
		error = null;
		try {
			const next = await getWritebackTargets(playlistId, selected.vendor as WritebackVendor, selected.target_mode as 'live', selected.target_path);
			if (!requestGate.isCurrent(ticket, selection())) return;
			targets = next.targets;
		} catch (cause) {
			if (requestGate.isCurrent(ticket, selection())) error = message(cause);
		} finally {
			if (requestGate.isCurrent(ticket, selection())) loading = false;
		}
	}

	async function refreshPlan(preserveResult = false): Promise<void> {
		clearPlan(!preserveResult);
		const selected = selection();
		if (selected === null || !selected.target_id) return;
		const ticket = requestGate.capture(selected);
		loading = true;
		error = null;
		try {
			const next = await getWritebackPlan(playlistId, selected.vendor as WritebackVendor, selected.target_mode as 'live', selected.target_path, selected.target_id);
			if (!requestGate.isCurrent(ticket, selection())) return;
			plan = next;
		} catch (cause) {
			if (requestGate.isCurrent(ticket, selection())) error = message(cause);
		} finally {
			if (requestGate.isCurrent(ticket, selection())) loading = false;
		}
	}

	function changeVendor(vendor: WritebackVendor): void {
		selectedVendor = vendor;
		void refreshTargets();
	}

	function changeTarget(): void {
		void refreshPlan();
	}

	async function onApply(): Promise<void> {
		if (writebackRefusal !== null) return;
		const selected = selection();
		const activePlan = plan;
		if (selected === null || !confirmed || activePlan === null || !planMatchesSelection(activePlan, selected)) {
			error = 'The reviewed plan no longer matches the selected native target. Review a new plan before applying.';
			confirmed = false;
			return;
		}
		const ticket = requestGate.capture(selected);
		loading = true;
		error = null;
		try {
			const result = await applyWriteback(playlistId, activePlan);
			if (!requestGate.isCurrent(ticket, selection())) return;
			applyResult = result;
			await refreshPlan(true);
		} catch (cause) {
			if (requestGate.isCurrent(ticket, selection())) error = message(cause);
		} finally {
			if (requestGate.isCurrent(ticket, selection())) loading = false;
		}
	}

	async function onRollback(): Promise<void> {
		// No gate check here on purpose: undo stays reachable in one-way import
		// mode. The evidence check below is what keeps it honest.
		const selected = selection();
		const activePlan = plan;
		const activeResult = applyResult;
		if (selected === null || activePlan === null || activeResult === null || !canRollbackWriteback(rollbackConfirmed, activePlan, activeResult, selected)) {
			error = rollbackConfirmed
				? 'The rollback evidence no longer matches the selected native target.'
				: 'Confirm rollback of this exact native playlist before restoring it.';
			return;
		}
		const ticket = requestGate.capture(selected);
		loading = true;
		try {
			await rollbackWriteback(playlistId, activeResult, activePlan);
			if (!requestGate.isCurrent(ticket, selection())) return;
			applyResult = null;
			await refreshPlan(true);
		} catch (cause) {
			if (requestGate.isCurrent(ticket, selection())) error = message(cause);
		} finally {
			if (requestGate.isCurrent(ticket, selection())) loading = false;
		}
	}
</script>

<a href={`/playlist/${playlistId}`}>&larr; back</a><h2>Write back to rekordbox / djay</h2>
{#if error}<p class="warn">{error}</p>{/if}
{#if capabilities}<div class="vendor-picker">{#each capabilities as cap (cap.vendor)}<label><input type="radio" name="vendor" value={cap.vendor} disabled={!cap.available || loading} checked={selectedVendor === cap.vendor} onchange={() => changeVendor(cap.vendor)} /> {cap.vendor}{#if !cap.available}<span class="reason"> ({cap.reason})</span>{/if}</label>{/each}</div>{/if}
{#if selectedVendor}<label>Vendor playlist <select bind:value={targetId} disabled={loading} onchange={changeTarget}><option value="">Choose an exact vendor playlist</option>{#each targets as target (target.playlist_id)}<option value={target.playlist_id}>{target.name} ({target.playlist_id})</option>{/each}</select></label>{/if}
{#if loading}<p>Working...</p>{:else if plan}<h3>Plan: {plan.target_name} -&gt; {plan.vendor}</h3><p>Target ID: <code>{plan.target_id}</code></p><p>Plan token: <code>{plan.plan_token}</code></p>{#if plan.is_noop}<p>No changes -- target already matches.</p>{:else if writebackPlanMutation(plan) === 'reorder'}<p class="warn">Membership is unchanged, but this live write will reorder the native playlist to the exact source order.</p>{:else}<p>Add {plan.added.length}, remove {plan.removed.length}.</p>{/if}{#if plan.unresolved.length}<p class="warn">Unmapped tracks block this apply: {plan.unresolved.join(', ')}</p>{/if}<label><input type="checkbox" bind:checked={confirmed} disabled={loading} /> I confirm this exact native playlist and revision</label><button onclick={onApply} disabled={loading || !confirmed || plan.is_noop || plan.unresolved.length > 0 || writebackRefusal !== null} title={writebackRefusal ?? `Write this membership to ${plan.vendor}`}>Apply to {plan.vendor}</button>{#if writebackRefusal}<p class="warn">{writebackRefusal}</p>{/if}{/if}
{#if applyResult}<h3>Result</h3>{#if applyResult.applied}<p>Applied: +{applyResult.added.length} / -{applyResult.removed.length}; backup: <code>{applyResult.backup_id}</code></p><label><input type="checkbox" bind:checked={rollbackConfirmed} disabled={loading} /> I confirm rollback of this exact native playlist</label><button onclick={onRollback} disabled={loading || !rollbackConfirmed} title="Restore the native playlist from the backup. Undo stays available in one-way import mode.">Rollback this write</button>{:else}<p class="warn">Not applied: {applyResult.error}</p>{/if}{/if}
<style>.vendor-picker { display:flex; gap:1rem; margin-bottom:1rem; }.reason { color:var(--muted); }.warn { color:var(--error, #c0392b); } select { margin-left:.5rem; }</style>
