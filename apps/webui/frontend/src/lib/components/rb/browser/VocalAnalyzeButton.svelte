<!--
	Stems column: trigger vocal analysis from an existing stem bundle
	(POST /api/v1/vocals/analyze, mode from-stems -- CPU, no demucs).
	Inert with a tooltip until a ready stem bundle exists (PARITY-08 /
	issue #1038 -- the agent-native parity hole left by closing #377).
-->
<script lang="ts">
	import { analyzeVocalsFromStems, RbApiError, type StemSummary } from '$lib/rb/api-rb';
	import {
		isStableIdOnLoadedDeck,
		refreshLoadedDeckVocalsFromServer
	} from '$lib/rb/loaded-deck-vocals-refresh';
	import { pushToast } from '$lib/stores.svelte';
	import { vocalsAnalyzeOutcome, vocalsAnalyzeRefusal } from '$lib/rb/vocals-analyze-refusal';
	import { requestTracksRefresh } from '$lib/rb/tracks-refresh-request';

	interface Props {
		stableId: string;
		stems: StemSummary | null;
	}

	const { stableId, stems }: Props = $props();

	let busy = $state(false);
	let serverRefusal = $state<string | null>(null);

	const refusal = $derived(vocalsAnalyzeRefusal(stems));
	const disabled = $derived(refusal !== null || busy);
	const tip = $derived(refusal ?? 'run vocal analysis from this stem bundle (CPU, no demucs)');

	async function onclick(e: MouseEvent): Promise<void> {
		e.stopPropagation();
		if (disabled) return;
		busy = true;
		try {
			const result = await analyzeVocalsFromStems(stableId);
			const outcome = vocalsAnalyzeOutcome(stableId, result);
			serverRefusal = outcome.category;
			pushToast(outcome.message, outcome.toastKind);
			if (outcome.refresh) {
				if (isStableIdOnLoadedDeck(stableId)) void refreshLoadedDeckVocalsFromServer([stableId]);
				requestTracksRefresh([stableId]);
			}
		} catch (err) {
			const msg = err instanceof RbApiError ? err.message : String(err);
			pushToast(`Vocal analysis failed: ${msg}`, 'error');
		} finally {
			busy = false;
		}
	}
</script>

<button
	class="va-btn"
	class:rb-inert={refusal !== null}
	{disabled}
	title={tip}
	aria-label={tip}
	data-testid="vocal-analyze-btn"
	data-agent-refusal={serverRefusal ?? undefined}
	{onclick}
>
	<svg viewBox="0 0 16 16" width="10" height="10" aria-hidden="true">
		<path
			d="M8 1.5a2.2 2.2 0 0 0-2.2 2.2v4a2.2 2.2 0 0 0 4.4 0v-4A2.2 2.2 0 0 0 8 1.5z"
			fill="none"
			stroke="currentColor"
			stroke-width="1.2"
		/>
		<path
			d="M4.2 7.5v.4a3.8 3.8 0 0 0 7.6 0v-.4M8 11.7v2"
			fill="none"
			stroke="currentColor"
			stroke-width="1.2"
		/>
	</svg>
</button>

<style>
	.va-btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 14px;
		height: 14px;
		padding: 0;
		margin-left: 3px;
		background: none;
		border: none;
		color: color-mix(in srgb, #6ec8ff 70%, transparent);
		cursor: pointer;
		flex: none;
	}
	.va-btn:hover:not(:disabled) {
		color: #cfefff;
	}
	.va-btn.rb-inert {
		color: color-mix(in srgb, var(--rb-text, #888) 30%, transparent);
		cursor: default;
	}
	.va-btn:disabled {
		cursor: default;
	}
</style>
