<script lang="ts">
	import { onMount } from 'svelte';
	import { pushToast } from '$lib/stores.svelte';
	import {
		getRecorderStatus,
		getSession,
		getTimeline,
		getTransitions,
		listSessions,
		recoverRecorder,
		sessionAudioUrl,
		startRecorder,
		stopRecorder,
		type RecorderStatus,
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
	let selectionRequest = 0;

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
		try {
			const [nextSession, nextTimeline, nextTransitions] = await Promise.all([
				getSession(sessionId),
				getTimeline(sessionId),
				getTransitions(sessionId)
			]);
			if (requestId === selectionRequest) {
				selected = { ...nextSession, transitions: nextTransitions };
				timeline = nextTimeline;
			}
		} catch (error) {
			pushToast(`Failed to open session: ${error}`, 'error');
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
				sources: ['djay_monitor']
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
		return typeof title === 'string' ? title : event.action.replaceAll('_', ' ');
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
		<span class="external-owner">Owned by process {recorder.pid}</span>
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
			<span>{sessions.length}</span>
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
				<strong>{new Date(session.started_at).toLocaleDateString()}</strong>
				<span>{formatDuration(session.duration_s)} · {session.event_count} events</span>
				<small>{session.transition_count} transitions · {session.share_state}</small>
			</button>
		{/each}
	</aside>

	<main class="session-detail">
		{#if selected}
			<div class="detail-heading">
				<div>
					<p class="eyebrow">{selected.summary.session_id}</p>
					<h3>{formatDuration(selected.summary.duration_s)} session</h3>
				</div>
				<span class="privacy">{selected.summary.share_state}</span>
			</div>

			<section class="replay" aria-label="Session replay">
				<h4>Replay</h4>
				{#if selected.segments.length === 0}
					<p class="empty">No recorded audio segments.</p>
				{/if}
				{#each selected.segments as segment (segment.name)}
					<div class="segment">
						<div>
							<strong>Segment {Math.floor(segment.start_t_s / 60) + 1}</strong>
							<small>{segment.name}</small>
						</div>
						<audio controls preload="metadata" src={sessionAudioUrl(selected.summary.session_id, segment.name)}></audio>
					</div>
				{/each}
			</section>

			<section class="timeline" aria-label="Session timeline">
				<div class="section-title">
					<h4>Timeline</h4>
					<span>{timeline.length} events</span>
				</div>
				{#each timeline as event, index (`${event.timestamp_s}-${index}`)}
					<div class="event-row">
						<time>{event.timestamp_s.toFixed(1)}s</time>
						<span class="event-marker"></span>
						<div>
							<strong>{eventTitle(event)}</strong>
							<small>{event.deck ? `Deck ${event.deck} · ` : ''}{event.source}</small>
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
	.rec-panel { background: linear-gradient(110deg, #171c25, #10141b); border: 1px solid #2a313d; border-radius: 12px; padding: 1rem 1.15rem; margin-bottom: 1.25rem; }
	.rec-state { display: flex; align-items: center; gap: 0.8rem; margin-right: auto; }
	.rec-state p, label span, small { display: block; color: var(--muted); font-size: 0.75rem; }
	.rec-dot { width: 12px; height: 12px; border-radius: 50%; background: #58606c; box-shadow: 0 0 0 5px #252a32; }
	.rec-dot.live { background: #ff4d57; box-shadow: 0 0 0 5px #57262b; }
	.rec-panel label { display: flex; align-items: center; gap: 0.65rem; }
	.rec-panel input { width: 92px; }
	.record { background: #e83c47; border-color: #ff6570; color: white; font-weight: 800; letter-spacing: 0.08em; }
	.stop { border-color: #ff6570; color: #ff9198; }
	.external-owner { color: var(--muted); font-size: 0.8rem; }
	.session-layout { display: grid; grid-template-columns: minmax(230px, 290px) minmax(0, 1fr); gap: 1rem; min-height: 520px; }
	.session-list, .session-detail { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 1rem; }
	.section-title { margin-bottom: 0.75rem; }
	.section-title span, .privacy { border: 1px solid #303744; border-radius: 999px; color: var(--muted); font-size: 0.72rem; padding: 0.18rem 0.5rem; }
	.session-card { display: block; text-align: left; width: 100%; padding: 0.8rem; margin-bottom: 0.5rem; background: transparent; }
	.session-card span { display: block; margin: 0.3rem 0; color: #c5ccd6; font-size: 0.82rem; }
	.session-card.selected { background: #202731; border-color: var(--accent-dim); box-shadow: inset 3px 0 var(--accent); }
	.session-detail { padding: 1.2rem 1.35rem; }
	.detail-heading { border-bottom: 1px solid var(--border); padding-bottom: 1rem; }
	.replay, .timeline { margin-top: 1.25rem; }
	.replay h4 { margin-bottom: 0.65rem; }
	.segment { border: 1px solid var(--border); border-radius: 8px; padding: 0.7rem 0.8rem; margin-top: 0.5rem; }
	.segment audio { height: 34px; max-width: 55%; }
	.event-row { display: grid; grid-template-columns: 52px 12px 1fr; gap: 0.65rem; min-height: 50px; align-items: start; }
	.event-row time { color: var(--muted); font-family: ui-monospace, monospace; font-size: 0.75rem; padding-top: 0.12rem; text-align: right; }
	.event-marker { position: relative; width: 8px; height: 8px; margin-top: 0.28rem; border-radius: 50%; background: var(--accent); }
	.event-marker::after { content: ''; position: absolute; top: 10px; bottom: -38px; left: 3px; width: 1px; background: #303744; }
	.event-row:last-child .event-marker::after { display: none; }
	.detail-empty { display: grid; place-items: center; min-height: 420px; }
	@media (max-width: 850px) {
		.session-layout { grid-template-columns: 1fr; }
		.rec-panel { align-items: flex-end; flex-wrap: wrap; }
		.segment { align-items: flex-start; flex-direction: column; }
		.segment audio { max-width: 100%; width: 100%; }
	}
</style>
