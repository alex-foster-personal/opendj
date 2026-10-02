<script lang="ts">
	import { onMount } from 'svelte';
	import { pushToast } from '$lib/stores.svelte';
	import { writeToastReport } from '$lib/toast-report';
	import {
		acknowledgeSoundcloudExport,
		getRecorderStatus,
		getMetadataShare,
		getSession,
		getSoundcloudExport,
		getTimeline,
		getTransitions,
		listSessions,
		publishMetadataShare,
		recoverRecorder,
		sessionAudioUrl,
		startRecorder,
		stopRecorder,
		type RecorderStatus,
		type MetadataShare,
		type SessionDetail,
		type SessionSummary,
		type TimelineEvent,
		type Transition
	} from './sets-api';

	type SessionView = SessionDetail & { transitions: Transition[] };

	let recorder = $state<RecorderStatus>({
		active: false,
		session_id: null,
		pid: null,
		owned: false,
		recoverable: false
	});
	let sessions = $state<SessionSummary[]>([]);
	let selected = $state<SessionView | null>(null);
	let timeline = $state<TimelineEvent[]>([]);
	let busy = $state(false);
	let deviceIndex = $state('');
	let shareLink = $state<string | null>(null);
	let selectionRequest = 0;
	let exportBusy = $state(false);
	let exportOpened = $state(false);
	let exportAcknowledged = $state(false);
	let exportReminder = $state('');
	let exportComment = $state('');
	let exportTrackCount = $state(0);

	function resetSoundcloudExport(): void {
		exportBusy = false;
		exportOpened = false;
		exportAcknowledged = false;
		exportReminder = '';
		exportComment = '';
		exportTrackCount = 0;
	}

	onMount(loadSurface);

	async function loadSurface(): Promise<void> {
		busy = true;
		try {
			const [nextRecorder, nextSessions] = await Promise.all([
				getRecorderStatus(),
				listSessions()
			]);
			recorder = nextRecorder;
			sessions = nextSessions;
			if (selected) {
				await selectSession(selected.summary.session_id);
			} else if (nextSessions[0]) {
				await selectSession(nextSessions[0].session_id);
			}
		} catch (error) {
			pushToast(`Failed to load sessions: ${error}`, 'error');
		} finally {
			busy = false;
		}
	}

	async function selectSession(sessionId: string): Promise<void> {
		const requestId = ++selectionRequest;
		resetSoundcloudExport();
		try {
			const [nextSession, nextTimeline, nextTransitions] = await Promise.all([
				getSession(sessionId),
				getTimeline(sessionId),
				getTransitions(sessionId)
			]);
			if (requestId === selectionRequest) {
				selected = { ...nextSession, transitions: nextTransitions };
				timeline = nextTimeline;
				shareLink = null;
				if (nextSession.summary.share_state === 'shared_cloud') {
					void loadExistingShare(sessionId, requestId);
				}
			}
		} catch (error) {
			pushToast(`Failed to open session: ${error}`, 'error');
		}
	}

	async function loadExistingShare(sessionId: string, requestId: number): Promise<void> {
		try {
			const existing = await getMetadataShare(sessionId);
			if (requestId === selectionRequest) shareLink = existing.share_url;
		} catch (error) {
			pushToast(`Failed to load share link: ${error}`, 'error');
		}
	}

	async function publishShare(): Promise<void> {
		if (!selected) return;
		const sessionId = selected.summary.session_id;
		const requestId = selectionRequest;
		busy = true;
		try {
			const metadataShare: MetadataShare = await publishMetadataShare(sessionId);
			if (requestId !== selectionRequest || selected?.summary.session_id !== sessionId) return;
			selected = {
				...selected,
				summary: { ...selected.summary, share_state: metadataShare.share_state }
			};
			shareLink = metadataShare.share_url;
			pushToast('Metadata-only share link is ready. Recorded audio stays local.');
		} catch (error) {
			pushToast(`Failed to create share link: ${error}`, 'error');
		} finally {
			busy = false;
		}
	}

	async function openSoundcloudExport(): Promise<void> {
		if (!selected) return;
		exportBusy = true;
		try {
			const payload = await getSoundcloudExport(selected.summary.session_id);
			exportOpened = true;
			exportAcknowledged = false;
			exportReminder = payload.licensing_reminder;
			exportComment = '';
			exportTrackCount = payload.tracklist.length;
		} catch (error) {
			pushToast(`SoundCloud export failed: ${error}`, 'error');
		} finally {
			exportBusy = false;
		}
	}

	async function acknowledgeSoundcloudRights(): Promise<void> {
		if (!selected) return;
		exportBusy = true;
		try {
			const payload = await acknowledgeSoundcloudExport(selected.summary.session_id);
			exportAcknowledged = true;
			exportReminder = payload.licensing_reminder;
			exportComment = payload.comment ?? '';
			exportTrackCount = payload.tracklist.length;
		} catch (error) {
			pushToast(`SoundCloud export failed: ${error}`, 'error');
		} finally {
			exportBusy = false;
		}
	}

	async function copySoundcloudComment(): Promise<void> {
		try {
			await writeToastReport(
				exportComment,
				typeof navigator === 'undefined' ? undefined : navigator.clipboard,
				typeof window === 'undefined' ? false : window.isSecureContext
			);
			pushToast('Tracklist copied');
		} catch (error) {
			pushToast(`Copy failed: ${error}`, 'error');
		}
	}

	async function startRecording(): Promise<void> {
		const parsedDeviceIndex = Number(deviceIndex);
		if (deviceIndex.trim() === '' || !Number.isInteger(parsedDeviceIndex) || parsedDeviceIndex < 0) {
			pushToast('Enter the ffmpeg audio input index before recording.', 'error');
			return;
		}
		busy = true;
		try {
			recorder = await startRecorder({
				session_id: null,
				ffmpeg_device_idx: parsedDeviceIndex,
				// opendj_decks records OUR own decks; the browser emitter installed
				// on /performance posts their state to /api/sets/deck-observations
				// while this session is live. Without it in this list REC captures
				// djay only and an Open DJ set records zero tracks.
				sources: ['djay_monitor', 'opendj_decks']
			});
			pushToast(`Recording ${recorder.session_id}`);
		} catch (error) {
			pushToast(`REC failed: ${error}`, 'error');
		} finally {
			busy = false;
		}
	}

	async function stopRecording(): Promise<void> {
		if (!recorder.session_id) return;
		busy = true;
		try {
			recorder = await stopRecorder(recorder.session_id);
			await loadSurface();
			pushToast('Recording stopped and session finalized.');
		} catch (error) {
			pushToast(`Stop failed: ${error}`, 'error');
		} finally {
			busy = false;
		}
	}

	async function recoverRecording(): Promise<void> {
		if (!recorder.session_id || recorder.pid === null) return;
		busy = true;
		try {
			recorder = await recoverRecorder(recorder.session_id, recorder.pid);
			await loadSurface();
			pushToast('Stale recording finalized.');
		} catch (error) {
			pushToast(`Recovery failed: ${error}`, 'error');
		} finally {
			busy = false;
		}
	}

	function formatDuration(seconds: number | null): string {
		if (seconds === null) return 'active';
		const hours = Math.floor(seconds / 3600);
		const minutes = Math.floor((seconds % 3600) / 60);
		return `${hours}h ${minutes.toString().padStart(2, '0')}m`;
	}

	function eventTitle(event: TimelineEvent): string {
		const title = event.value.title;
		if (typeof title === 'string' && title.trim() !== '') return title;
		if (event.track_stable_id) return event.track_stable_id;
		return event.action.replaceAll('_', ' ');
	}

	function trackEvents(): TimelineEvent[] {
		return timeline.filter((event) => event.action === 'track_loaded');
	}
</script>

<svelte:head><title>Sessions / REC - music-dj-tools</title></svelte:head>

<header class="page-header">
	<div>
		<p class="eyebrow">SETS</p>
		<h2>Sessions / REC</h2>
		<p class="subtitle">Capture a set, inspect its event timeline, and replay real audio segments.</p>
	</div>
	<button onclick={loadSurface} disabled={busy}>Refresh</button>
</header>

<section class="rec-panel" aria-label="Recording controls">
	<div class="rec-state">
		<span class:live={recorder.active} class="rec-dot"></span>
		<div>
			<strong>{recorder.active ? 'Recording' : 'Recorder ready'}</strong>
			<p>{recorder.session_id ?? 'No active session'}</p>
		</div>
	</div>
	{#if recorder.active && recorder.owned}
		<button class="stop" onclick={stopRecording} disabled={busy}>Stop recording</button>
	{:else if recorder.active && recorder.recoverable}
		<button class="stop" onclick={recoverRecording} disabled={busy}>Finalize stale session</button>
	{:else if recorder.active}
		<span class="external-owner" title="Operating-system process id of the recorder that owns this capture">Owned by process {recorder.pid}</span>
	{:else}
		<label>
			<span>ffmpeg input index</span>
			<input bind:value={deviceIndex} inputmode="numeric" placeholder="Required" aria-label="ffmpeg input index" />
		</label>
		<button class="record" onclick={startRecording} disabled={busy || deviceIndex.trim() === ''}>REC</button>
	{/if}
</section>

<div class="session-layout">
	<aside class="session-list" aria-label="Recorded sessions">
		<div class="section-title">
			<h3>Recorded sessions</h3>
			<span title="Finalized recorded sessions listed below">{sessions.length}</span>
		</div>
		{#if sessions.length === 0}
			<p class="empty">No finalized sessions found.</p>
		{/if}
		{#each sessions as session (session.session_id)}
			<button
				class:selected={selected?.summary.session_id === session.session_id}
				class="session-card"
				onclick={() => selectSession(session.session_id)}
			>
				<strong title={`Session start date, local time (recorded ${session.started_at})`}>{new Date(session.started_at).toLocaleDateString()}</strong>
				<span title="Session length (hours and minutes) and the number of events in its timeline">{formatDuration(session.duration_s)} · {session.event_count} events</span>
				<small title="Detected deck-to-deck transitions in this session, and its sharing state">{session.transition_count} transitions · {session.share_state}</small>
			</button>
		{/each}
	</aside>

	<main class="session-detail">
		{#if selected}
			<div class="detail-heading">
				<div>
					<p class="eyebrow">{selected.summary.session_id}</p>
					<h3 title="Session length, hours and minutes; active means it is still recording">{formatDuration(selected.summary.duration_s)} session</h3>
				</div>
				<span class="privacy">{selected.summary.share_state}</span>
			</div>

			<section class="set-summary" aria-label="Set summary">
				<div title="Track loads (track_loaded events) in this session's timeline">
					<strong>{trackEvents().length}</strong><span>tracks played</span>
				</div>
				<div title="Deck-to-deck transitions the detector found in this session">
					<strong>{selected.transitions.length}</strong><span>transitions detected</span>
				</div>
				<div title="Session length, hours and minutes">
					<strong>{formatDuration(selected.summary.duration_s)}</strong><span>on the floor</span>
				</div>
			</section>

			<section class="share-panel" aria-label="Metadata-only sharing">
				<div>
					<h4>Share this set</h4>
					<p>Publish the real track order, timings, and transitions. Recorded audio never leaves this machine.</p>
				</div>
				{#if shareLink}
					<label class="share-url">
						<span>Share link</span>
						<input aria-label="Share link" readonly value={shareLink} onclick={(event) => event.currentTarget.select()} />
					</label>
					<a class="share-open" href={shareLink} target="_blank" rel="noreferrer">Open shared view</a>
				{:else}
					<button class="share-create" onclick={publishShare} disabled={busy}>
						Create metadata-only share link
					</button>
				{/if}
			</section>

			<section class="replay" aria-label="Session replay">
				<h4>Replay</h4>
				{#if selected.segments.length === 0}
					<p class="empty">No recorded audio segments.</p>
				{/if}
				{#each selected.segments as segment (segment.name)}
					<div class="segment">
						<div>
							<strong title={`Recorded audio segment starting ${segment.start_t_s.toFixed(0)} s into the session; the number is the minute it starts in`}>Segment {Math.floor(segment.start_t_s / 60) + 1}</strong>
							<small>{segment.name}</small>
						</div>
						<audio controls preload="metadata" src={sessionAudioUrl(selected.summary.session_id, segment.name)}></audio>
					</div>
				{/each}
			</section>

			<section class="soundcloud-export" aria-label="SoundCloud tracklist">
				<h4>SoundCloud tracklist</h4>
				<p class="export-lead">Metadata only. Open DJ does not upload audio or offer takeover.</p>
				{#if !exportOpened}
					<button onclick={openSoundcloudExport} disabled={exportBusy || busy}>
						SoundCloud tracklist
					</button>
				{:else}
					<p class="licensing-reminder">{exportReminder}</p>
					{#if exportTrackCount === 0}
						<p class="empty">This session has no track_loaded rows in its timeline.</p>
					{:else if !exportAcknowledged}
						<button onclick={acknowledgeSoundcloudRights} disabled={exportBusy}>
							I own the rights or I will check SoundCloud's terms
						</button>
					{:else}
						<textarea readonly rows="8" aria-label="SoundCloud tracklist comment">{exportComment}</textarea>
						<button onclick={copySoundcloudComment} disabled={exportComment === ''}>Copy</button>
					{/if}
				{/if}
			</section>

			<section class="timeline" aria-label="Session timeline">
				<div class="section-title">
					<h4>Timeline</h4>
					<span title="Events recorded in this session's timeline">{timeline.length} events</span>
				</div>
				{#each timeline as event, index (`${event.timestamp_s}-${index}`)}
					<div class="event-row">
						<time title="Seconds from the start of the session to this event">{event.timestamp_s.toFixed(1)}s</time>
						<span class="event-marker"></span>
						<div>
							<strong>{eventTitle(event)}</strong>
							<small>{event.deck ? `Deck ${event.deck} · ` : ''}{event.source}</small>
						</div>
					</div>
				{/each}
			</section>

			<section class="transitions" aria-label="Detected transitions">
				<div class="section-title">
					<h4>Detected transitions</h4>
					<span title="Deck-to-deck transitions the detector found in this session">{selected.transitions.length}</span>
				</div>
				{#if selected.transitions.length === 0}
					<p class="empty">No transitions were recorded for this set.</p>
				{/if}
				{#each selected.transitions as transition (transition.idx)}
					<div class="transition-row">
						<time title="Seconds from the start of the session to the transition">{transition.t_change_s.toFixed(1)}s</time>
						<div>
							<strong>{transition.from_track ?? transition.from_deck ?? 'Unknown'} → {transition.to_track ?? transition.to_deck ?? 'Unknown'}</strong>
							<small title="Predicted transition type, and the detector's confidence in it (0-100%)">{transition.predicted_class} · {Math.round(transition.confidence * 100)}% confidence</small>
						</div>
					</div>
				{/each}
			</section>
		{:else}
			<p class="empty detail-empty">Select a recorded session to inspect it.</p>
		{/if}
	</main>
</div>

<style>
	.page-header, .rec-panel, .detail-heading, .section-title, .segment {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
	}
	.page-header { margin-bottom: 1.25rem; }
	h2, h3, h4, p { margin: 0; }
	.eyebrow { color: var(--accent); font-size: 0.72rem; font-weight: 700; letter-spacing: 0.18em; }
	.subtitle, .empty { color: var(--muted); margin-top: 0.35rem; }
	.rec-panel { background: linear-gradient(110deg, var(--surface-raised), var(--surface)); border: 1px solid var(--border); border-radius: 12px; padding: 1rem 1.15rem; margin-bottom: 1.25rem; }
	.rec-state { display: flex; align-items: center; gap: 0.8rem; margin-right: auto; }
	.rec-state p, label span, small { display: block; color: var(--muted); font-size: 0.75rem; }
	.rec-dot { width: 12px; height: 12px; border-radius: 50%; background: var(--muted); box-shadow: 0 0 0 5px var(--surface-raised); }
	.rec-dot.live { background: var(--danger); box-shadow: 0 0 0 5px color-mix(in srgb, var(--danger) 30%, var(--surface)); }
	.rec-panel label { display: flex; align-items: center; gap: 0.65rem; }
	.rec-panel input { width: 92px; }
	.record { background: var(--danger); border-color: var(--danger); color: var(--on-danger); font-weight: 800; letter-spacing: 0.08em; }
	.stop { border-color: var(--danger); color: var(--danger); }
	.external-owner { color: var(--muted); font-size: 0.8rem; }
	.session-layout { display: grid; grid-template-columns: minmax(230px, 290px) minmax(0, 1fr); gap: 1rem; min-height: 520px; }
	.session-list, .session-detail { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 1rem; }
	.section-title { margin-bottom: 0.75rem; }
	.section-title span, .privacy { border: 1px solid var(--border); border-radius: 999px; color: var(--muted); font-size: 0.72rem; padding: 0.18rem 0.5rem; }
	.session-card { display: block; text-align: left; width: 100%; padding: 0.8rem; margin-bottom: 0.5rem; background: transparent; }
	.session-card span { display: block; margin: 0.3rem 0; color: var(--fg); font-size: 0.82rem; }
	.session-card.selected { background: var(--surface-raised); border-color: var(--accent-dim); box-shadow: inset 3px 0 var(--accent); }
	.session-detail { padding: 1.2rem 1.35rem; }
	.detail-heading { border-bottom: 1px solid var(--border); padding-bottom: 1rem; }
	.set-summary { display: grid; grid-template-columns: repeat(3, 1fr); gap: 0.65rem; margin-top: 1.25rem; }
	.set-summary div { background: var(--surface-raised); border: 1px solid var(--border); border-radius: 9px; padding: 0.75rem; }
	.set-summary strong, .set-summary span { display: block; }
	.set-summary strong { font-size: 1.15rem; color: var(--fg); }
	.set-summary span { color: var(--muted); font-size: 0.74rem; margin-top: 0.2rem; }
	.share-panel { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 0.8rem 1rem; background: linear-gradient(110deg, color-mix(in srgb, var(--info) 18%, var(--surface)), var(--surface)); border: 1px solid color-mix(in srgb, var(--info) 55%, var(--border)); border-radius: 10px; padding: 0.9rem 1rem; margin-top: 1.25rem; }
	.share-panel h4, .share-panel p { margin: 0; }
	.share-panel p { color: var(--muted); font-size: 0.8rem; margin-top: 0.3rem; }
	.share-create, .share-open { background: var(--info); border-color: var(--info); color: var(--on-info); font-weight: 700; text-decoration: none; white-space: nowrap; }
	.share-url { grid-column: 1 / -1; display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center; gap: 0.6rem; }
	.share-url span { color: var(--muted); font-size: 0.75rem; }
	.share-url input { min-width: 0; width: 100%; color: var(--fg); font-family: ui-monospace, monospace; font-size: 0.72rem; }
	.replay, .timeline, .transitions, .soundcloud-export { margin-top: 1.25rem; }
	.soundcloud-export h4 { margin-bottom: 0.45rem; }
	.export-lead, .licensing-reminder { color: var(--muted); font-size: 0.85rem; margin: 0 0 0.75rem; max-width: 62ch; }
	.licensing-reminder { color: var(--fg); }
	.soundcloud-export textarea {
		display: block;
		width: 100%;
		margin: 0.75rem 0;
		padding: 0.7rem 0.8rem;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 8px;
		color: inherit;
		font-family: ui-monospace, monospace;
		font-size: 0.82rem;
		resize: vertical;
	}
	.replay h4 { margin-bottom: 0.65rem; }
	.segment { border: 1px solid var(--border); border-radius: 8px; padding: 0.7rem 0.8rem; margin-top: 0.5rem; }
	.segment audio { height: 34px; max-width: 55%; }
	.event-row { display: grid; grid-template-columns: 52px 12px 1fr; gap: 0.65rem; min-height: 50px; align-items: start; }
	.event-row time { color: var(--muted); font-family: ui-monospace, monospace; font-size: 0.75rem; padding-top: 0.12rem; text-align: right; }
	.event-marker { position: relative; width: 8px; height: 8px; margin-top: 0.28rem; border-radius: 50%; background: var(--accent); }
	.event-marker::after { content: ''; position: absolute; top: 10px; bottom: -38px; left: 3px; width: 1px; background: var(--border); }
	.event-row:last-child .event-marker::after { display: none; }
	.transition-row { display: grid; grid-template-columns: 52px minmax(0, 1fr); gap: 0.65rem; padding: 0.55rem 0; border-top: 1px solid var(--border); }
	.transition-row time { color: var(--muted); font-family: ui-monospace, monospace; font-size: 0.75rem; text-align: right; }
	.transition-row small { margin-top: 0.25rem; }
	.detail-empty { display: grid; place-items: center; min-height: 420px; }
	@media (max-width: 850px) {
		.session-layout { grid-template-columns: 1fr; }
		.rec-panel { align-items: flex-end; flex-wrap: wrap; }
		.segment { align-items: flex-start; flex-direction: column; }
		.segment audio { max-width: 100%; width: 100%; }
		.set-summary { grid-template-columns: 1fr; }
		.share-panel { grid-template-columns: 1fr; }
		.share-create, .share-open { justify-self: start; }
	}
</style>
