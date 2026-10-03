<script lang="ts">
	import { onMount } from 'svelte';
	import {
		capabilities,
		eventsRefusal,
		jobsRefusal,
		progressRefusal
	} from '$lib/api/capabilities.svelte';
	import {
		getConnectionState,
		getHello,
		getLastSeq,
		getRetryDelayMs,
		subscribeConnectionState,
		type ConnectionState
	} from '$lib/api/events-bus';
	import { health } from '$lib/stores.svelte';
	import { staleness } from './health-history';
	import { fetchWorktreePorts, type WorktreePorts } from './diagnostics-api';
	import LibraryOpsPanel from './LibraryOpsPanel.svelte';

	let busState = $state<ConnectionState>(getConnectionState());
	let ports = $state<WorktreePorts | null>(null);
	let portsError = $state<string | null>(null);
	let portsLoading = $state(true);
	let now = $state(Date.now());

	const age = $derived(staleness(health.lastOkAt, now));
	const historyNewestFirst = $derived([...health.history].reverse());

	function formatTime(at: number): string {
		return new Date(at).toLocaleTimeString();
	}

	function lockLabel(holder: string | null): string {
		return holder === null ? 'lock: free' : `lock: ${holder}`;
	}

	function syncthingLabel(peers: number | null, folder: string | null): string {
		if (peers === null || folder === null) return '-';
		return `syncthing: ${peers} peers - ${folder}`;
	}

	onMount(() => {
		void capabilities.probe();
		const unsubscribe = subscribeConnectionState((state) => {
			busState = state;
		});
		const tick = setInterval(() => {
			now = Date.now();
		}, 1000);
		fetchWorktreePorts().then(
			(loaded) => {
				ports = loaded;
				portsError = null;
			},
			(exc) => {
				portsError = exc instanceof Error ? exc.message : String(exc);
			}
		).finally(() => {
			portsLoading = false;
		});
		return () => {
			unsubscribe();
			clearInterval(tick);
		};
	});
</script>

<section class="panel">
	<h3>Capability probe</h3>
	<p class="sub">
		One GET /api/v1/health decides which daemon is serving this SPA. The flavor
		and handshake fields are memoized after the first successful probe.
	</p>
	<p>
		<strong>flavor:</strong> <code>{capabilities.flavor}</code>
	</p>
	<p>
		<strong>probed at:</strong>
		{capabilities.probedAt ?? 'not yet'}
	</p>
	{#if capabilities.error}
		<p class="warn">{capabilities.error}</p>
	{/if}
	{#if capabilities.handshake}
		<p><strong>handshake:</strong></p>
		<ul class="kv">
			<li><code>contract_rev</code>: {capabilities.handshake.contract_rev}</li>
			<li><code>engine_version</code>: {capabilities.handshake.engine_version}</li>
			<li><code>boot_id</code>: {capabilities.handshake.boot_id}</li>
		</ul>
	{:else if capabilities.flavor === 'legacy'}
		<p class="sub">legacy daemon (no handshake fields)</p>
	{/if}
	<ul class="kv">
		<li>events: {eventsRefusal() ?? 'offered'}</li>
		<li>jobs: {jobsRefusal() ?? 'offered'}</li>
		<li>progress ledger: {progressRefusal() ?? 'offered'}</li>
	</ul>
</section>

<section class="panel">
	<h3>Event bus</h3>
	{#if eventsRefusal() !== null}
		<p class="sub">{eventsRefusal()}</p>
	{:else}
		<p><strong>state:</strong> <code>{busState}</code></p>
		{#if getHello()}
			<ul class="kv">
				<li><code>contract_rev</code>: {getHello()?.contract_rev}</li>
				<li><code>engine_version</code>: {getHello()?.engine_version}</li>
				<li><code>seq_start</code>: {getHello()?.seq_start}</li>
				<li><code>topics</code>: {getHello()?.topics.join(', ')}</li>
			</ul>
		{:else}
			<p class="sub">no hello yet</p>
		{/if}
		<p><strong>last seq:</strong> {getLastSeq() ?? 'none'}</p>
		<p><strong>retry delay:</strong> {getRetryDelayMs()} ms</p>
	{/if}
</section>

<section class="panel">
	<h3>Health</h3>
	<p class="sub">
		Same fields the topbar prints, with last-success age and in-session history.
		Polling is owned by the root layout (30s).
	</p>
	{#if health.lastError}
		<div class="fatal">{health.lastError}</div>
	{/if}
	{#if health.lastOkAt !== null}
		<p>
			{#if age.stale}<strong>STALE</strong> {/if}
			last success {age.ageMs === null ? 'unknown' : `${Math.round(age.ageMs / 1000)}s`} ago
		</p>
	{/if}
	{#if health.data}
		<p class="readouts">
			<span class="readout">{health.data.state_db.tracks} tracks</span>
			<span class="sep"> · </span>
			<span class="readout">{health.data.state_db.playlists} playlists</span>
			<span class="sep"> · </span>
			<span class="readout">
				{#if health.data.cloud.lock_holder}
					lock: {health.data.cloud.lock_holder.holder}
				{:else}
					lock: free
				{/if}
			</span>
			{#if health.data.syncthing}
				<span class="sep"> · </span>
				<span class="readout">
					syncthing: {health.data.syncthing.peers_connected} peers -
					{health.data.syncthing.folder_state}
				</span>
			{/if}
			<span class="sep"> · </span>
			<span class="readout">bind: {health.data.bind_host}</span>
		</p>
	{:else if health.lastOkAt === null}
		<p class="sub">no successful health read this session yet</p>
	{/if}
	{#if historyNewestFirst.length === 0}
		<p class="sub">no successful health read this session yet</p>
	{:else}
		<table class="history">
			<thead>
				<tr>
					<th>time</th>
					<th>tracks</th>
					<th>playlists</th>
					<th>lock</th>
					<th>syncthing</th>
					<th>bind</th>
				</tr>
			</thead>
			<tbody>
				{#each historyNewestFirst as row (row.at)}
					<tr>
						<td>{formatTime(row.at)}</td>
						<td>{row.tracks}</td>
						<td>{row.playlists}</td>
						<td>{lockLabel(row.lockHolder)}</td>
						<td>{syncthingLabel(row.syncthingPeers, row.syncthingFolder)}</td>
						<td>{row.bindHost}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
</section>

<section class="panel">
	<h3>Worktree ports</h3>
	<p class="sub">
		The reserved pair for this worktree, matching
		<code>python -m apps.webui.port_config show</code>.
	</p>
	{#if portsLoading}
		<p class="sub">Loading ports...</p>
	{:else if portsError}
		<div class="fatal">{portsError}</div>
	{:else if ports}
		<pre class="ports" title="Matches just webui-ports output">backend  http://127.0.0.1:{ports.backend}
frontend http://127.0.0.1:{ports.frontend}
proxy    {ports.api_proxy_target}</pre>
	{/if}
</section>

<LibraryOpsPanel />

<style>
	.panel {
		max-width: 1180px;
		margin-bottom: 1.5rem;
	}
	h3 {
		font-size: 1rem;
		color: var(--accent);
		margin: 0 0 0.25rem 0;
	}
	.sub {
		color: var(--muted);
		font-size: 0.85rem;
		margin: 0 0 0.5rem 0;
		max-width: 90ch;
	}
	.warn {
		color: var(--danger);
		font-size: 0.85rem;
	}
	.fatal {
		background: var(--danger);
		color: #fff;
		padding: 1rem 1.25rem;
		border-radius: 8px;
		font-weight: 600;
		white-space: pre-wrap;
		line-height: 1.5;
		margin: 0.5rem 0;
	}
	code {
		background: var(--chip-bg);
		padding: 0.05rem 0.35rem;
		border-radius: 4px;
	}
	.kv {
		margin: 0.25rem 0 0.5rem 1.25rem;
		padding: 0;
		font-size: 0.85rem;
	}
	.readouts {
		font-size: 0.9rem;
	}
	.sep {
		color: var(--muted);
	}
	.history {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.8rem;
		margin-top: 0.5rem;
	}
	.history th,
	.history td {
		border: 1px solid var(--border, #1c222c);
		padding: 0.25rem 0.5rem;
		text-align: left;
	}
	.history th {
		color: var(--muted);
		font-weight: 600;
	}
	.ports {
		background: var(--chip-bg);
		padding: 0.75rem 1rem;
		border-radius: 6px;
		font-size: 0.85rem;
		overflow-x: auto;
		user-select: all;
	}
</style>
