<script lang="ts">
	/**
	 * Read-only analytics over real Phase 12 track_loaded events.
	 * All controls serialize to GET /api/play-analytics for agent parity.
	 */
	import { onMount } from 'svelte';
	import { fetchPlayAnalytics } from './analytics-api';
	import { type PlayAnalyticsResponse, type ShareState } from './types';

	const LIMIT = 50;
	let data = $state<PlayAnalyticsResponse | null>(null);
	let error = $state<string | null>(null);
	let loading = $state(true);
	let shareState = $state<ShareState | ''>('');
	let requestRevision = 0;

	async function load(): Promise<void> {
		const revision = ++requestRevision;
		loading = true;
		data = null;
		try {
			const response = await fetchPlayAnalytics(shareState === '' ? null : shareState, LIMIT);
			if (revision === requestRevision) {
				data = response;
				error = null;
			}
		} catch (caught) {
			if (revision === requestRevision) {
				error = caught instanceof Error ? caught.message : String(caught);
			}
		} finally {
			if (revision === requestRevision) loading = false;
		}
	}

	function applyShareState(event: Event): void {
		const value = (event.currentTarget as HTMLSelectElement).value;
		if (
			value !== '' &&
			value !== 'private' &&
			value !== 'shared_local' &&
			value !== 'shared_cloud'
		) {
			throw new Error(`Unknown share state: ${value}`);
		}
		shareState = value;
		void load();
	}

	function formatDuration(seconds: number | null): string {
		if (seconds === null) return 'Active';
		const hours = Math.floor(seconds / 3600);
		const minutes = Math.floor((seconds % 3600) / 60);
		return `${hours}h ${minutes.toString().padStart(2, '0')}m`;
	}

	function formatDate(timestamp: string): string {
		return new Intl.DateTimeFormat(undefined, {
			dateStyle: 'medium',
			timeStyle: 'short'
		}).format(new Date(timestamp));
	}

	const maxPlayCount = $derived(data?.top_tracks[0]?.play_count ?? 1);

	onMount(() => void load());
</script>

<svelte:head>
	<title>Play analytics</title>
</svelte:head>

<section class="analytics-page">
	<header>
		<div>
			<p class="eyebrow">SESSION HISTORY</p>
			<h2>Play analytics</h2>
			<p class="subtitle">Track loads captured by the local set event store.</p>
		</div>
		<label>
			<span>Visibility</span>
			<select value={shareState} onchange={applyShareState} aria-label="Session visibility">
				<option value="">All sessions</option>
				<option value="private">Private</option>
				<option value="shared_local">Shared local</option>
				<option value="shared_cloud">Shared cloud</option>
			</select>
		</label>
	</header>

	{#if error}
		<div class="error" role="alert">Play analytics unavailable: {error}</div>
	{/if}

	{#if data}
		<div class="summary" aria-label="Play summary">
			<div title="Recorded sessions that match the visibility filter">
				<strong>{data.summary.sessions}</strong><span>sessions</span>
			</div>
			<div title="Track loads (track_loaded events) across those sessions; a load heard for less than the minimum audible time is not counted">
				<strong>{data.summary.plays}</strong><span>plays</span>
			</div>
			<div title="Distinct tracks loaded at least once across those sessions">
				<strong>{data.summary.unique_tracks}</strong><span>unique tracks</span>
			</div>
			<div title="Total length of finished sessions, hours and minutes; a session still recording is not counted">
				<strong>{formatDuration(data.summary.completed_duration_s)}</strong>
				<span>completed time</span>
			</div>
		</div>

		<div class="panels">
			<section class="panel">
				<div class="panel-title"><h3>Recent sessions</h3><span title={`Sessions listed below (at most ${LIMIT}, newest first)`}>{data.sessions.length} shown</span></div>
				{#if data.sessions.length === 0}
					<p class="empty">No sessions match this visibility filter.</p>
				{:else}
					<div class="table-wrap">
						<table>
							<thead><tr><th>Started</th><th>Duration</th><th>Plays</th><th>Unique</th><th>Visibility</th></tr></thead>
							<tbody>
								{#each data.sessions as session (session.session_id)}
									<tr>
										<td><strong title={`Session start, local time (recorded ${session.started_at})`}>{formatDate(session.started_at)}</strong><small>{session.session_id}</small></td>
										<td title="Session length, hours and minutes; Active means it is still recording">{formatDuration(session.duration_s)}</td>
										<td title="Counted plays (deck loads) in this session">{session.play_count}</td>
										<td title="Distinct tracks loaded in this session">{session.unique_track_count}</td>
										<td><span class="state">{session.share_state.replace('_', ' ')}</span></td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				{/if}
			</section>

			<section class="panel top-tracks">
				<div class="panel-title"><h3>Most played</h3><span>track loads</span></div>
				{#if data.top_tracks.length === 0}
					<p class="empty">No track loads were captured.</p>
				{:else}
					<ol>
						{#each data.top_tracks as track (track.stable_id)}
							<li>
								<div class="track-label">
									<span><strong>{track.title ?? track.stable_id}</strong><small>{track.artist ?? 'Unknown artist'}</small></span>
									<b title="Counted plays of this track (deck loads) across sessions matching the filter">{track.play_count}</b>
								</div>
								<div class="bar"><span style={`width: ${(track.play_count / maxPlayCount) * 100}%`}></span></div>
							</li>
						{/each}
					</ol>
				{/if}
			</section>
		</div>
	{:else if loading && !error}
		<p class="empty">Loading recorded sessions...</p>
	{/if}
</section>

<style>
	.analytics-page { max-width: 1180px; margin: 0 auto; }
	header { display: flex; align-items: end; justify-content: space-between; gap: 1rem; margin-bottom: 1.3rem; }
	h2 { margin: 0.1rem 0 0.25rem; font-size: 1.75rem; letter-spacing: -0.03em; }
	.eyebrow { margin: 0; color: var(--accent); font-size: 0.68rem; font-weight: 800; letter-spacing: 0.16em; }
	.subtitle, .empty { margin: 0; color: var(--muted); }
	label { display: grid; gap: 0.3rem; color: var(--muted); font-size: 0.72rem; }
	label select { min-width: 150px; }
	.error { padding: 0.7rem 0.9rem; margin-bottom: 1rem; background: color-mix(in srgb, var(--danger) 15%, var(--surface)); border: 1px solid var(--danger); border-radius: 7px; }
	.summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border: 1px solid var(--border); background: var(--surface); border-radius: 9px; margin-bottom: 1rem; }
	.summary div { display: grid; gap: 0.15rem; padding: 1rem 1.15rem; border-right: 1px solid var(--border); }
	.summary div:last-child { border-right: 0; }
	.summary strong { font-size: 1.45rem; font-variant-numeric: tabular-nums; }
	.summary span, .panel-title span { color: var(--muted); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; }
	.panels { display: grid; grid-template-columns: minmax(0, 1.65fr) minmax(280px, 0.85fr); gap: 1rem; }
	.panel { border: 1px solid var(--border); background: var(--surface); border-radius: 9px; overflow: hidden; }
	.panel-title { display: flex; align-items: baseline; justify-content: space-between; padding: 0.8rem 1rem; border-bottom: 1px solid var(--border); }
	.panel-title h3 { margin: 0; font-size: 0.92rem; }
	.table-wrap { overflow-x: auto; }
	table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
	th { color: var(--muted); font-size: 0.68rem; font-weight: 600; text-align: left; text-transform: uppercase; letter-spacing: 0.05em; }
	th, td { padding: 0.72rem 0.8rem; border-bottom: 1px solid var(--border); }
	tbody tr:last-child td { border-bottom: 0; }
	td strong, td small, .track-label span, .track-label strong, .track-label small { display: block; }
	td small, .track-label small { margin-top: 0.2rem; color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.67rem; }
	.state { white-space: nowrap; padding: 0.15rem 0.45rem; background: var(--surface-raised); border: 1px solid var(--border); border-radius: 999px; font-size: 0.68rem; }
	ol { list-style: none; margin: 0; padding: 0.25rem 1rem 0.5rem; }
	li { padding: 0.65rem 0; border-bottom: 1px solid var(--border); }
	li:last-child { border-bottom: 0; }
	.track-label { display: flex; justify-content: space-between; align-items: center; gap: 0.8rem; }
	.track-label strong { font-size: 0.82rem; }
	.track-label b { color: var(--accent); font-variant-numeric: tabular-nums; }
	.bar { height: 3px; margin-top: 0.5rem; background: var(--surface-raised-hover); border-radius: 2px; overflow: hidden; }
	.bar span { display: block; height: 100%; background: var(--accent); }
	.panel > .empty { padding: 1rem; }
	@media (max-width: 900px) {
		.panels { grid-template-columns: 1fr; }
		.summary { grid-template-columns: repeat(2, 1fr); }
		.summary div:nth-child(2) { border-right: 0; }
		.summary div:nth-child(-n + 2) { border-bottom: 1px solid var(--border); }
	}
</style>
