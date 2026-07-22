<script lang="ts">
	// Text-command entry (text-command-entry, RECON-FEATURES.md apps/voice
	// section): the closest rekordbox-parity hook for apps/voice is the
	// top-bar mic/voice indicator area. This mounts a plain text input next
	// to it, submits to POST /api/v1/voice/probe (grammar-only, no mic, no
	// daemon), and renders the parsed intent inline. SEARCH results are
	// rendered here only - wiring the query into the browser pane search
	// box is a local follow-up (browser search state is per-pane, not a
	// shared store yet).
	import { probeVoiceCommand } from '$lib/rb/api-rb';
	import type { VoiceProbeResult } from '$lib/rb/api-rb';

	let text = $state('');
	let loading = $state(false);
	let result = $state<VoiceProbeResult | null>(null);
	let errorMessage = $state<string | null>(null);

	function summarize(r: VoiceProbeResult): string {
		if (r.intent === null) return `no match: "${r.transcript}"`;
		if (r.blocked) return `${r.intent} blocked: ${r.reason ?? 'destructive intent'}`;
		const query = typeof r.slots.query === 'string' ? ` "${r.slots.query}"` : '';
		return `${r.intent}${query}${r.reply ? ` -> ${r.reply}` : ''}`;
	}

	async function submit(): Promise<void> {
		const submitted = text.trim();
		if (!submitted || loading) return;
		loading = true;
		errorMessage = null;
		try {
			result = await probeVoiceCommand(submitted);
		} catch (e) {
			result = null;
			errorMessage = e instanceof Error ? e.message : 'voice probe failed';
		} finally {
			loading = false;
		}
	}

	function handleKeydown(e: KeyboardEvent): void {
		if (e.key === 'Enter') {
			e.preventDefault();
			void submit();
		}
	}

	const statusText = $derived(errorMessage ?? (result ? summarize(result) : ''));
	const statusTitle = $derived(
		errorMessage ?? (result ? JSON.stringify(result) : '')
	);
</script>

<div class="cmd-entry">
	<input
		class="cmd-input"
		type="text"
		placeholder="voice command..."
		spellcheck="false"
		autocomplete="off"
		bind:value={text}
		onkeydown={handleKeydown}
		aria-label="text command entry"
	/>
	{#if statusText}
		<span class="cmd-status" class:error={!!errorMessage} title={statusTitle}>
			{statusText}
		</span>
	{/if}
</div>

<style>
	.cmd-entry {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		min-width: 0;
	}

	.cmd-input {
		width: 130px;
		height: 18px;
		padding: 0 6px;
		background: #0a0c0f;
		border: 1px solid var(--rb-border);
		border-radius: 9px;
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		outline: none;
	}
	.cmd-input::placeholder {
		color: var(--rb-text-dim);
	}

	.cmd-status {
		max-width: 160px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
	}
	.cmd-status.error {
		color: var(--rb-red);
	}
</style>
