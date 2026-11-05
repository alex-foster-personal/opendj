<!--
	AUDIO-DEVICE-04/05 (I/O panel): reconnect a stalled Bluetooth main speaker,
	and the optional acoustic proof that the room really hears the master bus.
	Agent twin: window.__odjSpeakerLink (reconnect, startAcousticProof, state),
	the CLI `opendj audio_reconnect_speaker --confirm` and MCP audio_reconnect_speaker.
-->
<script lang="ts">
	import { reconnectSpeakerNow, speakerLink } from '$lib/rb/speaker-link.svelte';
	import type { ProofMic } from '$lib/rb/speaker-acoustic-proof';

	let mics = $state<ProofMic[] | null>(null);
	let chosen = $state('');
	let listening = $state(false);
	let micError = $state<string | null>(null);
	const reconnecting = $derived(speakerLink.status.phase === 'reconnecting');

	async function loadMics(): Promise<void> {
		micError = null;
		try {
			const runtime = await import('$lib/rb/speaker-acoustic-proof-runtime');
			mics = await runtime.listProofMics();
			if (mics.length === 0) {
				micError = 'No wired or USB mic with a visible name. Allow device access above, then list again. Bluetooth mics are never used.';
			} else if (chosen === '') {
				chosen = mics[0].deviceId;
			}
		} catch (error: unknown) {
			micError = error instanceof Error ? error.message : String(error);
		}
	}

	async function toggleProof(): Promise<void> {
		micError = null;
		const runtime = await import('$lib/rb/speaker-acoustic-proof-runtime');
		if (listening) {
			runtime.stopAcousticProof();
			listening = false;
			return;
		}
		const mic = mics?.find((candidate) => candidate.deviceId === chosen);
		if (mic === undefined) {
			micError = 'Choose a mic first.';
			return;
		}
		try {
			await runtime.startAcousticProof(mic);
			listening = true;
		} catch (error: unknown) {
			micError = error instanceof Error ? error.message : String(error);
		}
	}
</script>

<section class="hp-section sl" aria-label="Speaker link" data-speaker-link={speakerLink.status.phase}>
	<div class="sl-heading">
		<h3>Speaker link</h3>
		<button
			type="button"
			data-performance-control="reconnect-speaker"
			disabled={reconnecting}
			title="Disconnect and reconnect the Bluetooth main speaker, make it the output again and restart the room audio (about 5 s)"
			onclick={() => void reconnectSpeakerNow()}>{reconnecting ? 'Reconnecting...' : 'Reconnect speaker'}</button
		>
	</div>
	{#if speakerLink.status.message !== null}
		<p class="sl-status" role="status" data-speaker-link-message>{speakerLink.status.message}</p>
	{/if}
	<p class="sl-status">Proof by ear: a wired or USB mic (never Bluetooth) listens for the master bus in 2 s windows while a deck plays.</p>
	<div class="sl-row">
		{#if mics === null}
			<button type="button" onclick={() => void loadMics()}>List mics</button>
		{:else}
			<select aria-label="Acoustic proof microphone" bind:value={chosen} disabled={listening}>
				{#each mics as mic (mic.deviceId)}<option value={mic.deviceId}>{mic.label} ({mic.transport})</option>{/each}
			</select>
			<button type="button" data-performance-control="acoustic-proof" onclick={() => void toggleProof()} disabled={mics.length === 0}
				>{listening ? 'Stop listening' : 'Listen'}</button
			>
		{/if}
	</div>
	{#if speakerLink.acoustic !== null}
		<p class="sl-status" role="status" data-acoustic-proof={speakerLink.acoustic.verdict}>
			{speakerLink.acoustic.mic_label}: {speakerLink.acoustic.verdict === 'not_delivering'
				? 'not hearing the master bus'
				: speakerLink.acoustic.verdict === 'heard'
					? 'hears the master bus'
					: 'waiting for a playing deck'} (missed {speakerLink.acoustic.failed_windows} of {speakerLink.acoustic.windows_required})
		</p>
	{/if}
	{#if micError !== null || speakerLink.acoustic_error !== null}
		<p class="sl-error" role="alert">{micError ?? speakerLink.acoustic_error}</p>
	{/if}
</section>

<style>
	.sl { display: grid; gap: 4px; padding: 8px 0; border-bottom: 1px solid var(--rb-border, #333); }
	.sl-heading { display: flex; justify-content: space-between; align-items: center; gap: 6px; }
	.sl-heading h3 { margin: 0; font-size: inherit; }
	.sl-row { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
	.sl select { min-width: 0; max-width: 100%; font: inherit; }
	.sl button {
		background: var(--rb-panel-raised, #1a1e25);
		border: 1px solid var(--rb-border, #333);
		border-radius: 3px;
		color: var(--rb-text, #c8cdd2);
		padding: 3px 6px;
		font: inherit;
	}
	.sl button:disabled { opacity: 0.5; cursor: not-allowed; }
	.sl-status { margin: 0; color: var(--rb-text-dim, #838990); font-size: 10px; line-height: 1.35; overflow-wrap: anywhere; }
	.sl-error { margin: 0; color: var(--rb-warn, #e0a040); overflow-wrap: anywhere; }
</style>
