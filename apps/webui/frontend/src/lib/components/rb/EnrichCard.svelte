<!--
	ENRICH-01: the enrich-on-open card. One fixed card, bottom right, that says
	what the library still lacks and asks the one opt-in question (stems).

	Every word comes from `$lib/enrich/enrich-card` (pure, unit tested); this
	component only fetches and wires buttons. Agent parity, one endpoint per
	control:
	  load      GET  /api/v1/enrich/summary          (enrich_cli summary)
	  Retry     POST /api/v1/ahead-analysis/retry    (ahead_analysis_cli retry)
	  Separate  POST /api/v1/jobs (via StemsPrompt)
	  Never     PUT  /api/v1/enrich/decisions/stems  (enrich_cli decide)
	  Hide      session only, re-shown on the next open
-->
<script lang="ts">
	import { onMount } from 'svelte';
	import { api, unwrap } from '$lib/api/client';
	import StemsPrompt from '$lib/components/rb/StemsPrompt.svelte';
	import {
		type EnrichSummary,
		analysisLines,
		lyricsLine,
		offersRetry,
		stemsText
	} from '$lib/enrich/enrich-card';

	const REFRESH_MS = 30_000;
	const HIDE_KEY = 'odj.enrich-card.hidden';

	let summary = $state<EnrichSummary | null>(null);
	let loadError = $state<string | null>(null);
	let actionError = $state<string | null>(null);
	let hidden = $state(readHidden());
	let asking = $state(false);
	let busy = $state(false);

	const lines = $derived(summary ? [...analysisLines(summary), ...[lyricsLine(summary)].filter((l) => l !== null)] : []);
	const stemsLine = $derived(summary ? stemsText(summary.stems) : null);
	const visible = $derived(!hidden && (loadError !== null || (summary?.show ?? false)));

	function readHidden(): boolean {
		try {
			return sessionStorage.getItem(HIDE_KEY) === '1';
		} catch {
			return false;
		}
	}

	function hide(): void {
		hidden = true;
		try {
			sessionStorage.setItem(HIDE_KEY, '1');
		} catch {
			// Storage refused (private window): the card hides for this page only.
		}
	}

	async function load(): Promise<void> {
		try {
			summary = (await unwrap(api.GET('/api/v1/enrich/summary'))) as unknown as EnrichSummary;
			loadError = null;
		} catch (error) {
			loadError = `GET /api/v1/enrich/summary failed: ${error instanceof Error ? error.message : String(error)}`;
		}
	}

	async function act(call: () => Promise<unknown>): Promise<void> {
		busy = true;
		actionError = null;
		try {
			await call();
			await load();
		} catch (error) {
			actionError = error instanceof Error ? error.message : String(error);
		} finally {
			busy = false;
		}
	}

	const retry = (): Promise<void> => act(() => unwrap(api.POST('/api/v1/ahead-analysis/retry')));
	const never = (): Promise<void> =>
		act(() =>
			unwrap(
				api.PUT('/api/v1/enrich/decisions/{lane}', {
					params: { path: { lane: 'stems' } },
					body: { answer: 'never' }
				})
			)
		);

	onMount(() => {
		void load();
		const timer = setInterval(() => void load(), REFRESH_MS);
		return () => clearInterval(timer);
	});
</script>

{#if visible}
	<section class="enrich-card" aria-label="Library enrichment" data-testid="enrich-card">
		<header>
			<h3>Getting your library ready</h3>
			<button type="button" class="enrich-hide" onclick={hide} title="Hide until the app is next opened">Hide</button>
		</header>
		{#if loadError}
			<p class="enrich-line failed">{loadError}</p>
		{/if}
		{#each lines as line (line.lane + line.tone)}
			<p class="enrich-line {line.tone}" title={line.title ?? undefined} data-lane={line.lane} data-tone={line.tone}>
				{line.text}
			</p>
		{/each}
		{#if summary && offersRetry(summary)}
			<button type="button" onclick={retry} disabled={busy}>Retry failed analysis</button>
		{/if}
		{#if stemsLine}
			<p class="enrich-line {summary?.stems.state === 'ask' ? 'working' : 'unavailable'}" data-lane="stems">
				{stemsLine}
			</p>
		{/if}
		{#if summary?.stems.state === 'ask'}
			{#if asking}
				<StemsPrompt onenqueued={() => void load()} onskip={() => (asking = false)} />
			{:else}
				<div class="enrich-actions">
					<button type="button" class="enrich-go" onclick={() => (asking = true)}>Separate stems...</button>
					<button type="button" onclick={hide}>Not now</button>
					<button type="button" onclick={never} disabled={busy}>Never for this library</button>
				</div>
			{/if}
		{/if}
		{#if actionError}
			<p class="enrich-line failed">{actionError}</p>
		{/if}
	</section>
{/if}

<style>
	.enrich-card {
		position: fixed;
		right: 12px;
		bottom: 12px;
		z-index: 40;
		width: min(380px, calc(100vw - 24px));
		display: flex;
		flex-direction: column;
		gap: 6px;
		padding: 10px 12px;
		background: var(--rb-panel);
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
		font-size: var(--rb-fs-label);
		box-shadow: 0 4px 16px rgb(0 0 0 / 35%);
	}
	header {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}
	h3 {
		margin: 0;
		font-size: 13px;
		letter-spacing: 0.04em;
	}
	.enrich-line {
		margin: 0;
		line-height: 16px;
	}
	.enrich-line.working {
		color: var(--rb-text);
	}
	.enrich-line.note {
		color: var(--rb-text-dim);
	}
	.enrich-line.failed {
		color: var(--rb-red);
	}
	.enrich-line.unavailable {
		color: var(--rb-text-dim);
		font-style: italic;
	}
	.enrich-actions {
		display: flex;
		gap: 8px;
		flex-wrap: wrap;
	}
	button {
		font: inherit;
		font-size: 11px;
		padding: 4px 10px;
		background: var(--rb-panel);
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
		cursor: pointer;
		align-self: flex-start;
	}
	.enrich-go {
		border-color: var(--rb-accent);
	}
	.enrich-hide {
		padding: 2px 8px;
	}
</style>
