<script lang="ts">
	// Text-command entry (text-command-entry, RECON-FEATURES.md apps/voice
	// section): the closest rekordbox-parity hook for apps/voice is the
	// top-bar mic/voice indicator area. This mounts a plain text input next
	// to it, submits to POST /api/v1/voice/probe (grammar-only, no mic, no
	// daemon), and renders the parsed intent inline. SEARCH is forwarded
	// through the shared browser-search command path to the active pane.
	import { onMount } from 'svelte';
	import { probeVoiceCommand } from '$lib/rb/api-rb';
	import type { VoiceProbeResult } from '$lib/rb/api-rb';
	import {
		requestBrowserSearch,
		subscribeBrowserSearchResult,
		type BrowserSearchResult
	} from '$lib/rb/browser-search';

	let text = $state('');
	let loading = $state(false);
	let result = $state<VoiceProbeResult | null>(null);
	let browserSearchResult = $state<BrowserSearchResult | null>(null);
	let errorMessage = $state<string | null>(null);

	function summarize(r: VoiceProbeResult): string {
		if (r.intent === null) return `no match: "${r.transcript}"`;
		if (r.blocked) return `${r.intent} blocked: ${r.reason ?? 'destructive intent'}`;
		const query = typeof r.slots.query === 'string' ? ` "${r.slots.query}"` : '';
		const fallback = browserSearchResult === null
			? '; browser search result pending'
			: browserSearchResult.fallback
			? `; ${browserSearchResult.rowCount} result${browserSearchResult.rowCount === 1 ? '' : 's'} ignoring ${browserSearchResult.ignoredFilters.join(' and ')}`
			: '';
		return `${r.intent}${query}${r.reply ? ` -> ${r.reply}` : ''}${fallback}`;
	}

	async function submit(): Promise<void> {
		const submitted = text.trim();
		if (!submitted || loading) return;
		loading = true;
		errorMessage = null;
		browserSearchResult = null;
		try {
			result = await probeVoiceCommand(submitted);
			if (result.client_action === 'browser_search') {
				const query = result.slots.query;
				if (typeof query !== 'string') {
					throw new Error('voice probe browser_search action omitted query');
				}
				const request = requestBrowserSearch(query);
				browserSearchResult = request.result;
			}
		} catch (e) {
			result = null;
			errorMessage = e instanceof Error ? e.message : 'voice probe failed';
		} finally {
			loading = false;
		}
	}

	onMount(() =>
		subscribeBrowserSearchResult((request) => {
			browserSearchResult = request.result;
		})
	);

	function handleKeydown(e: KeyboardEvent): void {
		if (e.key === 'Enter') {
			e.preventDefault();
			void submit();
		}
	}

	const statusText = $derived(errorMessage ?? (result ? summarize(result) : ''));
	const statusTitle = $derived(
		errorMessage ?? (result ? summarize(result) : 'Voice command result')
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
