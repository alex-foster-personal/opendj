<script lang="ts">
	/**
	 * Phone remote, in the Audio I/O panel: pair phones on the LAN to drive decks.
	 *
	 * Every button calls the engine route an agent would call
	 * (/api/v1/performance/remote-control/...); status is the engine's, polled
	 * while the panel is open. QR entry with a Show big overlay is issue #10442.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { API_BASE } from '$lib/api/base';
	import { writeToastReport } from '$lib/toast-report';
	import {
		ENABLED_QR_TARGETS,
		REMOTE_CONTROL_ROUTES,
		pairedDate,
		relativeSeen,
		remoteControlRequest,
		remoteControlUrl,
		remoteQrImageUrl,
		remoteStatusLine,
		startStatusPoll,
		type QrTarget,
		type RemoteControlState
	} from '$lib/player/remote-control-core';

	let remoteState = $state<RemoteControlState | null>(null);
	let busy = $state(false);
	let error = $state<string | null>(null);
	let pollError = $state<string | null>(null);
	let copied = $state<string | null>(null);
	let bigTarget = $state<QrTarget | null>(null);
	let stopPoll: (() => void) | undefined;

	async function call(
		action: Parameters<typeof remoteControlRequest>[0],
		deviceId?: string
	): Promise<RemoteControlState> {
		const req = remoteControlRequest(action, deviceId);
		const init: RequestInit =
			req.body === undefined
				? { method: req.method }
				: {
						method: req.method,
						headers: { 'content-type': 'application/json' },
						body: JSON.stringify(req.body)
					};
		const response = await fetch(`${API_BASE}${REMOTE_CONTROL_ROUTES}${req.path}`, init);
		const parsed: unknown = await response.json().catch(() => null);
		if (!response.ok) {
			const detail =
				parsed !== null && typeof parsed === 'object'
					? (parsed as { detail?: unknown }).detail
					: undefined;
			throw new Error(
				`${req.method} ${req.path} answered ${response.status}: ${String(detail ?? 'no detail')}`
			);
		}
		return parsed as RemoteControlState;
	}

	async function refresh(): Promise<void> {
		try {
			remoteState = await call('status');
			pollError = null;
		} catch (failure) {
			pollError = failure instanceof Error ? failure.message : String(failure);
		}
	}

	async function act(
		action: Parameters<typeof remoteControlRequest>[0],
		deviceId?: string
	): Promise<void> {
		busy = true;
		error = null;
		copied = null;
		try {
			remoteState = await call(action, deviceId);
		} catch (failure) {
			error = failure instanceof Error ? failure.message : String(failure);
		} finally {
			busy = false;
		}
	}

	async function copy(text: string, what: 'code' | 'address'): Promise<void> {
		error = null;
		copied = null;
		if (typeof navigator === 'undefined') {
			error = 'clipboard unavailable outside the browser';
			return;
		}
		try {
			await writeToastReport(text, navigator.clipboard, window.isSecureContext);
			copied = what === 'code' ? 'Code copied' : 'Address copied';
		} catch (failure) {
			error = failure instanceof Error ? failure.message : String(failure);
		}
	}

	const listening = $derived(remoteState?.status === 'listening');
	const url = $derived(remoteState !== null ? remoteControlUrl(remoteState) : null);

	onMount(() => {
		void refresh();
		stopPoll = startStatusPoll(refresh);
	});
	onDestroy(() => stopPoll?.());
</script>

<div class="rc" data-remote-control>
	<h4
		title="Pair a phone on this network to drive the decks. Opens a LAN listener on its own port; the main engine stays local."
	>
		Phone remote
	</h4>
	<div class="rc-row">
		{#if listening}
			<button type="button" disabled={busy} onclick={() => act('stop')} data-remote-control-stop
				>Stop</button
			>
		{:else}
			<button
				type="button"
				disabled={busy}
				title="Open a LAN listener so phones on this network can pair with a code and drive the decks."
				onclick={() => act('start')}
				data-remote-control-start>Start</button
			>
		{/if}
		{#if remoteState !== null}
			<span
				class="rc-status"
				data-remote-control-status={remoteState.status}
				title="Wrong codes so far: {remoteState.wrong_codes} of {remoteState.max_wrong_codes}."
				>{remoteStatusLine(remoteState)}{#if remoteState.reopened_at_boot}
					(reopened at boot){/if}</span
			>
		{/if}
	</div>
	{#if listening}
		{#if remoteState !== null && remoteState.code !== null}
			{@const pairingCode = remoteState.code}
			<div class="rc-row rc-code-row">
				<strong class="rc-code" data-remote-control-code>{pairingCode}</strong>
				<button
					type="button"
					disabled={busy}
					onclick={() => copy(pairingCode, 'code')}
					data-remote-control-copy-code>Copy</button
				>
			</div>
			{#each ENABLED_QR_TARGETS as target (target)}
				<div class="rc-qr" data-remote-control-qr={target}>
					<img
						src="{API_BASE}{remoteQrImageUrl(target, pairingCode)}"
						alt="QR code for the phone remote URL"
						width="148"
						height="148"
					/>
					<button type="button" onclick={() => (bigTarget = target)} data-remote-control-show-big
						>Show big</button
					>
				</div>
			{/each}
		{/if}
		<div class="rc-row">
			{#if url !== null}
				<code data-remote-control-url>{url}</code>
				<button
					type="button"
					disabled={busy}
					onclick={() => copy(url, 'address')}
					data-remote-control-copy-url>Copy</button
				>
			{:else if remoteState !== null && remoteState.address_error !== null}
				<p class="rc-error" role="status">{remoteState.address_error}</p>
			{/if}
		</div>
		<button
			type="button"
			disabled={busy}
			title="Issue a new code; already paired phones stay paired."
			onclick={() => act('rotate')}
			data-remote-control-rotate>Rotate code</button
		>
		{#if remoteState !== null && remoteState.devices.length === 0}
			<p class="rc-status">No phones paired yet.</p>
		{/if}
	{/if}
	{#if remoteState !== null && remoteState.devices.length > 0}
		<ul data-remote-control-devices>
			{#each remoteState.devices as d (d.device_id)}
				<li data-remote-control-device={d.device_id}>
					<span>{d.device_name}</span>
					<span class="rc-meta" title={d.paired_at}>paired {pairedDate(d.paired_at)}</span>
					<span class="rc-meta" title={d.last_seen ?? 'never'}
						>last seen {relativeSeen(d.last_seen)}</span
					>
					<button
						type="button"
						disabled={busy}
						title="Unpair this phone; it must enter a new code to pair again."
						onclick={() => act('forget', d.device_id)}
						data-remote-control-forget>Forget</button
					>
				</li>
			{/each}
		</ul>
	{/if}
	{#if copied !== null}<p class="rc-status" role="status">{copied}</p>{/if}
	{#if error !== null}<p class="rc-error" role="status">{error}</p>{/if}
	{#if pollError !== null}<p class="rc-error" role="status">{pollError}</p>{/if}
</div>

{#if bigTarget !== null && remoteState !== null && remoteState.code}
	<div class="rc-big" role="dialog" aria-label="Phone remote QR" data-remote-control-big>
		<button type="button" class="rc-big-close" onclick={() => (bigTarget = null)}>Close</button>
		<img
			src="{API_BASE}{remoteQrImageUrl(bigTarget, remoteState.code)}"
			alt="Large QR code for the phone remote URL"
		/>
	</div>
{/if}

<style>
	.rc {
		display: grid;
		gap: 4px;
	}
	.rc h4 {
		margin: 0;
		font-size: inherit;
		font-weight: 600;
		cursor: help;
	}
	.rc-row {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 4px;
	}
	.rc-code-row {
		align-items: baseline;
	}
	.rc button {
		background: var(--rb-panel-raised, #1a1e25);
		border: 1px solid var(--rb-border, #333);
		border-radius: 3px;
		color: var(--rb-text, #c8cdd2);
		padding: 3px 6px;
		font: inherit;
	}
	.rc button:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.rc-code {
		font-size: 1.6em;
		letter-spacing: 0.08em;
		font-variant-numeric: tabular-nums;
		font-family: monospace;
	}
	.rc-status {
		margin: 0;
		color: var(--rb-text-dim, #838990);
		overflow-wrap: anywhere;
	}
	.rc-meta {
		color: var(--rb-text-dim, #838990);
		font-size: 0.92em;
	}
	.rc-error {
		margin: 0;
		color: var(--rb-warn, #e0a040);
		overflow-wrap: anywhere;
	}
	ul {
		margin: 0;
		padding: 0;
		list-style: none;
		display: grid;
		gap: 4px;
	}
	li {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
	}
	code {
		font-size: 0.92em;
		overflow-wrap: anywhere;
	}
	.rc-qr {
		display: flex;
		align-items: flex-end;
		gap: 0.75rem;
	}
	.rc-qr img {
		background: #fff;
		border-radius: 4px;
	}
	.rc-big {
		position: fixed;
		inset: 0;
		z-index: 10000;
		display: flex;
		align-items: center;
		justify-content: center;
		background: rgba(0, 0, 0, 0.85);
	}
	.rc-big img {
		width: min(80vw, 80vh);
		height: auto;
		background: #fff;
		padding: 1rem;
		border-radius: 8px;
	}
	.rc-big-close {
		position: absolute;
		top: 1rem;
		right: 1rem;
	}
</style>
