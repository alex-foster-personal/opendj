<script lang="ts">
	/**
	 * I18 remote speaker, in the Audio I/O panel: "Be a speaker" here, or send
	 * this machine's MAIN to a speaker elsewhere on the LAN.
	 *
	 * Every button calls the engine route an agent would call
	 * (/api/v1/performance/remote-speaker/...), so the panel and HTTP drive
	 * one path. Status is the engine's, read while the panel is open.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { API_BASE } from '$lib/api/base';
	import {
		REMOTE_SPEAKER_DELAY_TITLE,
		REMOTE_SPEAKER_ROUTES,
		isSpeakerAddress,
		isSpeakerCode,
		linkStatusLine,
		speakerStatusLine,
		type RemoteLinkState,
		type RemoteSpeakerState
	} from '$lib/player/remote-speaker-core';

	/** Panel-only status refresh, while the panel is open. */
	const STATUS_POLL_MS = 1_000;

	let speaker = $state<RemoteSpeakerState | null>(null);
	let link = $state<RemoteLinkState | null>(null);
	let address = $state('');
	let code = $state('');
	let busy = $state(false);
	let error = $state<string | null>(null);
	let poll: ReturnType<typeof setInterval> | undefined;

	async function call(path: string, method: 'GET' | 'POST', body?: unknown): Promise<unknown> {
		const init: RequestInit =
			body === undefined
				? { method }
				: { method, headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) };
		const response = await fetch(`${API_BASE}${REMOTE_SPEAKER_ROUTES}${path}`, init);
		const parsed: unknown = await response.json().catch(() => null);
		if (!response.ok) {
			const detail = parsed !== null && typeof parsed === 'object' ? (parsed as { detail?: unknown }).detail : undefined;
			throw new Error(`${method} ${path} answered ${response.status}: ${String(detail ?? 'no detail')}`);
		}
		return parsed;
	}

	async function refresh(): Promise<void> {
		try {
			speaker = (await call('/speaker', 'GET')) as RemoteSpeakerState;
			link = (await call('/link', 'GET')) as RemoteLinkState;
		} catch (failure) {
			error = failure instanceof Error ? failure.message : String(failure);
		}
	}

	async function act(path: string, body: unknown = {}): Promise<void> {
		busy = true;
		error = null;
		try {
			await call(path, 'POST', body);
		} catch (failure) {
			error = failure instanceof Error ? failure.message : String(failure);
		} finally {
			busy = false;
			await refresh();
		}
	}

	const speakerOn = $derived(speaker !== null && speaker.status !== 'off' && speaker.status !== 'locked_out');
	const linkLive = $derived(link !== null && (link.status === 'linked' || link.status === 'linking'));
	const canSend = $derived(!busy && isSpeakerAddress(address) && isSpeakerCode(code));

	onMount(() => {
		void refresh();
		poll = setInterval(() => void refresh(), STATUS_POLL_MS);
	});
	onDestroy(() => clearInterval(poll));
</script>

<div class="rs" data-remote-speaker>
	<h4 title={REMOTE_SPEAKER_DELAY_TITLE}>Remote speaker (delayed)</h4>
	<div class="rs-row">
		{#if speakerOn}
			<button type="button" disabled={busy} onclick={() => act('/speaker/stop')} data-remote-speaker-stop>Stop speaker</button>
		{:else}
			<button
				type="button"
				disabled={busy}
				title="Let another Open DJ machine on this network play its MAIN here. Opens a LAN listener for the speaker offer only."
				onclick={() => act('/speaker/start')}
				data-remote-speaker-start>Be a speaker</button
			>
		{/if}
		{#if speaker !== null}
			<span
				class="rs-status"
				data-remote-speaker-status={speaker.status}
				title="The address:port and 6-digit code a sender types in. Wrong codes so far: {speaker.wrong_codes} of {speaker.max_wrong_codes} allowed before the listener closes."
				>{speakerStatusLine(speaker)}</span
			>
		{/if}
	</div>
	<div class="rs-row">
		<input
			type="text"
			placeholder="address:port"
			aria-label="Speaker address and port"
			title="The speaker's address:port, as its screen shows it"
			bind:value={address}
			disabled={linkLive}
		/>
		<input
			type="text"
			inputmode="numeric"
			maxlength="6"
			placeholder="code"
			aria-label="Speaker code"
			title="The speaker's 6-digit code"
			bind:value={code}
			disabled={linkLive}
		/>
		{#if linkLive}
			<button type="button" disabled={busy} onclick={() => act('/unlink')} data-remote-speaker-unlink>Unlink</button>
		{:else}
			<button
				type="button"
				disabled={!canSend}
				title={canSend ? REMOTE_SPEAKER_DELAY_TITLE : 'Type the speaker address:port and its 6-digit code first'}
				onclick={() => act('/link', { address: address.trim(), code })}
				data-remote-speaker-send>Send to speaker</button
			>
		{/if}
	</div>
	{#if link !== null}
		<p class="rs-status" data-remote-link-status={link.status} title={REMOTE_SPEAKER_DELAY_TITLE}>{linkStatusLine(link)}</p>
	{/if}
	{#if error !== null}<p class="rs-error" role="status">{error}</p>{/if}
</div>

<style>
	.rs { display: grid; gap: 4px; }
	.rs h4 { margin: 0; font-size: inherit; font-weight: 600; cursor: help; }
	.rs-row { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
	.rs input { width: 150px; min-width: 0; font: inherit; }
	.rs input[inputmode='numeric'] { width: 52px; }
	.rs button {
		background: var(--rb-panel-raised, #1a1e25);
		border: 1px solid var(--rb-border, #333);
		border-radius: 3px;
		color: var(--rb-text, #c8cdd2);
		padding: 3px 6px;
		font: inherit;
	}
	.rs button:disabled { opacity: 0.5; cursor: not-allowed; }
	.rs-status { margin: 0; color: var(--rb-text-dim, #838990); overflow-wrap: anywhere; }
	.rs-error { margin: 0; color: var(--rb-warn, #e0a040); overflow-wrap: anywhere; }
</style>
