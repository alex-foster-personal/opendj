<script lang="ts">
	/**
	 * /cloudsync Fleet tab: enrollment state of every machine this state DB
	 * knows. HTTP twin of `python -m apps.sync_hub fleet --json`
	 * (GET /api/v1/cloudsync/fleet, local operator only).
	 *
	 * Also the enrollment-grant form, HTTP twin of `python -m apps.sync_hub
	 * grant` (POST /api/v1/cloudsync/enrollment-grants, hub and local operator
	 * only). The token is shown ONCE in a read-only field: it is never logged,
	 * toasted or persisted, and it is dropped when a new grant is requested.
	 */
	import { onMount } from 'svelte';

	import {
		getCloudSyncFleet,
		mintEnrollmentGrant,
		type FleetOut,
		type GrantOut
	} from '$lib/api-cloudsync-ops';

	import {
		ENROLL_COMMAND_HINT,
		grantErrorSentence,
		grantExpiryTitle,
		grantRequest
	} from './cloudsync-view';
	import CloudSyncFleetTabView from './CloudSyncFleetTabView.svelte';

	let fleet = $state<FleetOut | null>(null);
	let loadError = $state<string | null>(null);

	let grantEmail = $state('');
	let grantBusy = $state(false);
	let grantError = $state<string | null>(null);
	let grant = $state<GrantOut | null>(null);
	let copyState = $state<'idle' | 'copied' | 'failed'>('idle');

	async function createGrant(): Promise<void> {
		grant = null;
		grantError = null;
		copyState = 'idle';
		const decision = grantRequest(grantEmail);
		if (decision.kind === 'refuse') {
			grantError = decision.reason;
			return;
		}
		grantBusy = true;
		try {
			grant = await mintEnrollmentGrant(decision.body);
		} catch (exc) {
			grantError = grantErrorSentence(exc);
		} finally {
			grantBusy = false;
		}
	}

	async function copyToken(): Promise<void> {
		if (grant === null) return;
		try {
			await navigator.clipboard.writeText(grant.token);
			copyState = 'copied';
		} catch {
			copyState = 'failed';
		}
	}

	async function refresh(): Promise<void> {
		try {
			fleet = await getCloudSyncFleet();
			loadError = null;
		} catch (exc) {
			loadError = exc instanceof Error ? exc.message : String(exc);
		}
	}

	onMount(refresh);
</script>

<CloudSyncFleetTabView
	{fleet}
	{loadError}
	onRefresh={() => refresh()}
	bind:grantEmail
	{grantBusy}
	{grantError}
	{grant}
	{copyState}
	expiryTitle={grant === null ? '' : grantExpiryTitle(grant)}
	enrollHint={ENROLL_COMMAND_HINT}
	onCreateGrant={() => void createGrant()}
	onCopyToken={() => void copyToken()}
/>
