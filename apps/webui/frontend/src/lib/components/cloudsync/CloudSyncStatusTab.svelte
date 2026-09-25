<script lang="ts">
	/**
	 * /cloudsync Status tab: the truthful status panel, the per-machine config
	 * form, and a Sync now button. Owns the config and status it shows so the
	 * page stays wiring only. Outcomes render inline (role=status), readable
	 * by a person, an agent and the e2e alike. HTTP twins (agent parity):
	 *   GET  /api/v1/cloudsync/status   <- python -m apps.sync_hub status
	 *   GET/PUT /api/v1/cloudsync/config <- python -m apps.sync_hub config show|set
	 *   POST /api/v1/cloudsync/sync     <- python -m apps.sync_hub sync
	 */
	import { onMount } from 'svelte';

	import { getIdentityBacklog, getStatus, type CloudSyncStatus } from '$lib/api-cloudsync';
	import {
		getCloudSyncConfig,
		putCloudSyncConfig,
		runCloudSyncNow,
		type CloudSyncConfigOut
	} from '$lib/api-cloudsync-ops';
	import { uiPrefs } from '$lib/rb/prefs.svelte';

	import { warnForceSyncBypassesGate } from './cloudsync-toasts';
	import {
		CLOUDSYNC_TECHNICAL_DETAILS_LABEL,
		FORCE_SYNC_LABEL,
		STATUS_CHANGED_EVENT,
		configPutBody,
		envOverrideNotes,
		fetchUiMirrorForGate,
		forceSyncNowRequest,
		formFromConfig,
		identityBacklogNote,
		presentCloudSyncError,
		presentCloudSyncResultError,
		statusHeadline,
		syncNowRequest,
		syncRuntimeGateReason,
		type ConfigFormFields,
		type UiMirrorDecks
	} from './cloudsync-view';

	interface Notice {
		kind: 'ok' | 'error';
		text: string;
		/** Hover explanation, required whenever the text carries counts. */
		title?: string;
		/** Raw error text for the technical-details disclosure only. */
		details?: string;
	}

	/** Tell the status chip to re-read now instead of on its next poll. */
	function announceStatusChanged(): void {
		window.dispatchEvent(new CustomEvent(STATUS_CHANGED_EVENT));
	}

	let status = $state<CloudSyncStatus | null>(null);
	/** null = not yet loaded (or failed to load); the note stays hidden either way. */
	let identityBacklogCount = $state<number | null>(null);
	let config = $state<CloudSyncConfigOut | null>(null);
	let loadError = $state<string | null>(null);
	let form = $state<ConfigFormFields>({ enabled: false, hubUrl: '', machineName: '' });
	let saving = $state(false);
	let syncing = $state(false);
	let notice = $state<Notice | null>(null);
	let uiMirror = $state<{ decks?: UiMirrorDecks } | null>(null);

	const syncGate = $derived({
		appPosture: uiPrefs.app_posture,
		uiMirror
	});
	const syncDecision = $derived(syncNowRequest(config, syncGate));
	const forceSyncDecision = $derived(forceSyncNowRequest(config));
	const showForceSync = $derived(
		config?.effective.hub_url !== null &&
			config?.effective.hub_url !== undefined &&
			syncRuntimeGateReason(syncGate.appPosture, syncGate.uiMirror) !== null
	);

	function message(exc: unknown): string {
		return exc instanceof Error ? exc.message : String(exc);
	}

	/**
	 * Best-effort: the identity backlog is a supplementary telemetry note, not
	 * core status, so a failure here must never block the status panel from
	 * rendering (same reasoning as `fetchUiMirrorForGate`).
	 */
	async function fetchIdentityBacklog(): Promise<number | null> {
		try {
			return (await getIdentityBacklog()).hash_pending;
		} catch {
			return null;
		}
	}

	async function refresh(): Promise<void> {
		try {
			const [nextStatus, nextConfig, nextMirror, nextBacklog] = await Promise.all([
				getStatus(),
				getCloudSyncConfig(),
				fetchUiMirrorForGate(),
				fetchIdentityBacklog()
			]);
			status = nextStatus;
			config = nextConfig;
			uiMirror = nextMirror;
			identityBacklogCount = nextBacklog;
			loadError = null;
		} catch (exc) {
			loadError = message(exc);
		}
	}

	onMount(async () => {
		await refresh();
		if (config !== null) form = formFromConfig(config);
	});

	async function saveConfig(): Promise<void> {
		const decision = configPutBody(form);
		if (decision.kind === 'refuse') {
			notice = { kind: 'error', text: decision.reason };
			return;
		}
		saving = true;
		try {
			config = await putCloudSyncConfig(decision.body);
			form = formFromConfig(config);
			notice = { kind: 'ok', text: 'CloudSync config saved.' };
			status = await getStatus();
			announceStatusChanged();
		} catch (exc) {
			notice = { kind: 'error', text: `Failed to save CloudSync config: ${message(exc)}` };
		} finally {
			saving = false;
		}
	}

	async function syncNow(): Promise<void> {
		if (syncDecision.kind === 'refuse') {
			notice = { kind: 'error', text: syncDecision.reason };
			return;
		}
		syncing = true;
		try {
			const result = await runCloudSyncNow(syncDecision.body);
			notice = {
				kind: 'ok',
				text: `Sync ${result.digest_inconclusive ? 'inconclusive' : 'ok'}: pushed ${result.pushed}, pulled ${result.pulled}.`,
				title: `Rows this Sync now pushed to the hub (${result.pushed}) and pulled from it (${result.pulled}); ${result.rounds} round(s), hub sequence ${result.hub_seq}`
			};
		} catch (exc) {
			// The failure was journaled server-side; the refresh below shows it.
			const presented = presentCloudSyncError(exc);
			notice = {
				kind: 'error',
				text: `Sync failed: ${presented.summary}`,
				details: presented.details
			};
		} finally {
			syncing = false;
			await refresh();
			announceStatusChanged();
		}
	}

	async function forceSyncNow(): Promise<void> {
		if (forceSyncDecision.kind === 'refuse') {
			notice = { kind: 'error', text: forceSyncDecision.reason };
			return;
		}
		warnForceSyncBypassesGate();
		syncing = true;
		try {
			const result = await runCloudSyncNow(forceSyncDecision.body);
			notice = {
				kind: 'ok',
				text: `Sync ${result.digest_inconclusive ? 'inconclusive' : 'ok'}: pushed ${result.pushed}, pulled ${result.pulled}.`,
				title: `Rows this Force sync pushed to the hub (${result.pushed}) and pulled from it (${result.pulled}); ${result.rounds} round(s), hub sequence ${result.hub_seq}`
			};
		} catch (exc) {
			const presented = presentCloudSyncError(exc);
			notice = {
				kind: 'error',
				text: `Sync failed: ${presented.summary}`,
				details: presented.details
			};
		} finally {
			syncing = false;
			await refresh();
			announceStatusChanged();
		}
	}

	function yesNo(value: boolean): string {
		return value ? 'yes' : 'no';
	}

	const headline = $derived(statusHeadline(status));
	const backlogNote = $derived(identityBacklogNote(identityBacklogCount));
</script>

<section aria-label="CloudSync status" class="status-tab">
	{#if loadError !== null}
		<p class="err" role="alert">Failed to load CloudSync status: {loadError}</p>
	{/if}
	{#if notice !== null}
		<div
			class:err={notice.kind === 'error'}
			class:ok={notice.kind === 'ok'}
			role="status"
			title={notice.title}
			data-testid="cloudsync-notice"
		>
			<p class="notice-text">{notice.text}</p>
			{#if notice.details !== undefined}
				<details data-testid="cloudsync-notice-details">
					<summary>{CLOUDSYNC_TECHNICAL_DETAILS_LABEL}</summary>
					<pre class="technical-details">{notice.details}</pre>
				</details>
			{/if}
		</div>
	{/if}

	<div class="panel" data-testid="cloudsync-status-panel">
		<h3>Status</h3>
		{#if status === null}
			<p class="muted">Loading status...</p>
		{:else}
			<p class="headline" class:err={headline.tone === 'error'} class:warn={headline.tone === 'warn'} class:ok={headline.tone === 'ok'} data-testid="cloudsync-status-headline">
				{headline.text}
			</p>
			{#if backlogNote !== null}
				<p
					class="headline warn"
					data-testid="cloudsync-identity-backlog-note"
					title="apps/sync_hub/sync_set.py::count_hash_pending, live tracks offered as hash_pending on the hub"
				>
					{backlogNote}
				</p>
			{/if}
			<details class="tech-detail">
				<summary>Technical detail</summary>
				<dl>
					<dt>Configured</dt>
					<dd
						data-testid="cloudsync-status-configured"
						title={`Intent only: enabled from ${status.enabled_source}, hub URL from ${status.endpoint_source}`}
					>
						{yesNo(status.configured)}
					</dd>
					<dt>Running</dt>
					<dd
						data-testid="cloudsync-status-running"
						title="Evidence: true only while the background scheduler's heartbeat file is fresh"
					>
						{yesNo(status.running)}
					</dd>
					<dt>Heartbeat</dt>
					<dd title="UTC time of the last scheduler beat, fresh or stale">{status.heartbeat_at ?? 'none'}</dd>
					<dt>Hub</dt>
					<dd title={`Effective hub URL, from ${status.endpoint_source}`}>{status.endpoint ?? 'none'}</dd>
					<dt>Last result</dt>
					<dd data-testid="cloudsync-status-last-result">
						{#if status.last_result === null}
							none yet
						{:else if status.last_result.status === 'error'}
							{status.last_result.status}:
							{presentCloudSyncResultError(status.last_result, status.update_required).summary}
							<details data-testid="cloudsync-last-result-details">
								<summary>{CLOUDSYNC_TECHNICAL_DETAILS_LABEL}</summary>
								<pre class="technical-details">{status.last_result.message}</pre>
							</details>
						{:else}
							{status.last_result.status}: {status.last_result.message}
						{/if}
					</dd>
					<dt>Rows pending</dt>
					<dd title="Local changelog rows not yet pushed to the hub; blank when not measured">
						{status.rows_pending ?? 'not measured'}
					</dd>
					<dt>Hash pending</dt>
					<dd
						data-testid="cloudsync-status-hash-pending"
						title="Live tracks offered as hash_pending while awaiting content_hash (ADR-0068)"
					>
						{status.hash_pending ?? 'not measured'}
					</dd>
					<dt>Quarantined</dt>
					<dd
						data-testid="cloudsync-status-quarantined"
						title="Direct stamp faults and identity-dup losers only"
					>
						{status.quarantined ?? 'not measured'}
					</dd>
					<dt>Excluded total</dt>
					<dd
						data-testid="cloudsync-status-excluded-total"
						title="Every row held outside the sync set, including transitive parent and membership holds"
					>
						{status.excluded_total ?? 'not measured'}
					</dd>
					<dt>Signed in as</dt>
					<dd>{status.signed_in_as ?? 'nobody'}</dd>
				</dl>
				{#if status.reason}
					<p class="muted" data-testid="cloudsync-status-reason">{status.reason}</p>
				{/if}
			</details>
		{/if}
		<div class="actions">
			<button
				type="button"
				data-testid="cloudsync-sync-now"
				disabled={syncing || syncDecision.kind === 'refuse'}
				title={syncDecision.kind === 'refuse'
					? syncDecision.reason
					: `Run one push and pull against ${syncDecision.body.hub_url} now (POST /api/v1/cloudsync/sync)`}
				onclick={() => syncNow()}
			>
				{syncing ? 'Syncing...' : 'Sync now'}
			</button>
			{#if showForceSync}
				<button
					type="button"
					data-testid="cloudsync-force-sync"
					disabled={syncing || forceSyncDecision.kind === 'refuse'}
					title={`${FORCE_SYNC_LABEL} (POST /api/v1/cloudsync/sync with force=true)`}
					onclick={() => forceSyncNow()}
				>
					{syncing ? 'Syncing...' : FORCE_SYNC_LABEL}
				</button>
			{/if}
			<button type="button" title="Re-read status and config from the daemon" onclick={() => refresh()}>
				Refresh
			</button>
		</div>
	</div>

	<div class="panel">
		<h3>Recent results</h3>
		{#if status === null || status.recent_results.length === 0}
			<p class="muted">No sync attempts have completed yet.</p>
		{:else}
			<table class="library">
				<thead>
					<tr>
						<th>Finished (UTC)</th>
						<th>Status</th>
						<th>Pushed</th>
						<th>Pulled</th>
						<th>Message</th>
					</tr>
				</thead>
				<tbody>
					{#each status.recent_results as row (row.finished_at + row.status)}
						<tr data-testid="cloudsync-journal-row" data-status={row.status}>
							<td>{row.finished_at}</td>
							<td>{row.status}</td>
							<td title="Rows this attempt pushed to the hub">{row.pushed}</td>
							<td title="Rows this attempt pulled from the hub">{row.pulled}</td>
							<td>
								{#if row.status === 'error'}
									{presentCloudSyncResultError(row).summary}
									<details>
										<summary>{CLOUDSYNC_TECHNICAL_DETAILS_LABEL}</summary>
										<pre class="technical-details">{row.message}</pre>
									</details>
								{:else}
									{row.message}
								{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	</div>

	<form
		class="panel"
		data-testid="cloudsync-config-form"
		onsubmit={(e) => {
			e.preventDefault();
			void saveConfig();
		}}
	>
		<h3>Config (this machine)</h3>
		<label>
			<input type="checkbox" bind:checked={form.enabled} data-testid="cloudsync-config-enabled" />
			Enabled (the background scheduler syncs on its own)
		</label>
		<label>
			Hub URL
			<input
				type="url"
				placeholder="http://hub.tailnet:8686"
				bind:value={form.hubUrl}
				data-testid="cloudsync-config-hub-url"
			/>
		</label>
		<label>
			Machine name
			<input
				type="text"
				placeholder="hostname when blank"
				bind:value={form.machineName}
				data-testid="cloudsync-config-machine-name"
			/>
		</label>
		{#if config !== null}
			{#each envOverrideNotes(config) as note (note)}
				<p class="warn">{note}</p>
			{/each}
			<p class="muted" title="The file PUT /api/v1/cloudsync/config writes">{config.path}</p>
		{/if}
		<button type="submit" disabled={saving} data-testid="cloudsync-config-save">
			{saving ? 'Saving...' : 'Save config'}
		</button>
	</form>
</section>

<style>
	.status-tab {
		display: grid;
		gap: 1rem;
	}
	.panel {
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 0.75rem;
		background: var(--surface);
	}
	h3 {
		margin: 0 0 0.5rem;
		font-size: 0.95rem;
	}
	dl {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 4px 12px;
		margin: 0.5rem 0 0;
		font-size: 0.85rem;
	}
	.headline {
		margin: 0;
		font-size: 0.95rem;
		font-weight: 600;
		color: var(--fg);
	}
	.headline.ok {
		color: var(--accent);
	}
	.headline.warn {
		color: var(--warning, #b8860b);
	}
	.headline.err {
		color: var(--danger);
	}
	.tech-detail {
		margin-top: 0.6rem;
	}
	.tech-detail summary {
		cursor: pointer;
		color: var(--muted);
		font-size: 0.8rem;
	}
	dt {
		color: var(--muted);
	}
	dd {
		margin: 0;
	}
	.actions {
		display: flex;
		gap: 8px;
		margin-top: 0.75rem;
	}
	form label {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 0.35rem 0;
		font-size: 0.85rem;
	}
	form input[type='url'],
	form input[type='text'] {
		flex: 1;
		max-width: 28rem;
	}
	input,
	button {
		background: var(--bg);
		color: var(--fg);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 4px 8px;
		font-size: 0.85rem;
	}
	button {
		cursor: pointer;
	}
	button:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.muted {
		color: var(--muted);
		font-size: 0.8rem;
	}
	.warn {
		color: var(--warning, #b8860b);
		font-size: 0.8rem;
	}
	.ok {
		color: var(--accent);
	}
	.err {
		color: var(--danger);
	}
	.notice-text {
		margin: 0;
	}
	.technical-details {
		margin: 0.35rem 0 0;
		white-space: pre-wrap;
		word-break: break-word;
		font-size: 0.75rem;
		color: var(--muted);
	}
</style>
